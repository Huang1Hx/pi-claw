"""只负责显示去重，不改变完整消息的持久化、用量统计或宿主验收。"""

from typing import TextIO

from core.events import AgentEvent
from core.schemas import RunResult


class ReplyDisplay:
    """记录当前流与已经展示的完成候选；精确相同才去重，改写后的结论必须展示。"""

    MAX_COMPARE_CHARS = 100_000

    def __init__(self, output: TextIO) -> None:
        """绑定输出；比较缓冲有界，不保留跨回合生成内容。"""
        self.output = output
        self.reset()

    def reset(self) -> None:
        """新用户回合清空比较状态，不让上一回合文本吞掉下一回合回复。"""
        self.line_open = False
        self.current = ""
        self.overflow = False
        self.visible_candidate = ""
        self.purpose = "execution"
        self.pending_whitespace = ""

    def close_line(self) -> None:
        """事件、工具或正式答复插入前结束当前流式行。"""
        if self.line_open:
            print(file=self.output, flush=True)
            self.line_open = False
        self.pending_whitespace = ""

    def on_event(self, event: AgentEvent) -> None:
        """逐片显示仍待验收的文本，跟踪真实模型消息边界和工作流候选。"""
        if event.type == "text_delta":
            text = event.data.get("text", "")
            if not text:
                return
            if not self.overflow:
                if len(self.current) + len(text) <= self.MAX_COMPARE_CHARS:
                    self.current += text
                else:
                    self.current, self.overflow = "", True
            if not self.line_open:
                # 有些模型在工具调用前只推送换行；等到有正文才显示标签，保留正常缩进。
                if not text.strip():
                    self.pending_whitespace = (self.pending_whitespace + text)[
                        : self.MAX_COMPARE_CHARS
                    ]
                    return
                print("\ncandidate(stream)> ", end="", file=self.output, flush=True)
                print(self.pending_whitespace, end="", file=self.output, flush=True)
                self.pending_whitespace = ""
                self.line_open = True
            print(text, end="", file=self.output, flush=True)
            return
        # usage/reasoning 等纯进度不切断正文，避免同一流出现多段重复标签。
        if event.type in {"reasoning_progress", "usage_updated"}:
            return
        self.close_line()
        if event.type == "model_routed":
            self.current, self.overflow = "", False
            self.purpose = event.data.get("purpose", "execution")
            if self.purpose in {"coding", "execution"}:
                self.visible_candidate = ""
        elif event.type == "workflow_artifact" and event.data.get("kind") == "coding_candidate":
            candidate = event.data["data"].get("summary", "")
            self.visible_candidate = (
                candidate if self.current == candidate and not self.overflow else ""
            )
        elif event.type == "model_reply":
            final = event.data.get("final_text")
            if final is not None and final == self.current and not self.overflow:
                self.visible_candidate = final
            elif final is None and self.purpose != "reviewing":
                self.visible_candidate = ""
            self.current, self.overflow = "", False

    def finish(self, result: RunResult) -> None:
        """相同正文不重印；工作流只追加审查结论，错误仍由 REPL 明确显示。"""
        self.close_line()
        final = result.final_text
        if not final:
            return
        shown = self.visible_candidate
        if shown and final == shown:
            if result.ok:
                print("完成。", file=self.output)
            return
        if shown and final.startswith(shown + "\n\n审查结论："):
            print(final[len(shown) :].strip() + ("\n完成。" if result.ok else ""), file=self.output)
            return
        label = "assistant" if result.ok else "candidate(unverified)"
        print(f"\n{label}> {final}\n", file=self.output)
