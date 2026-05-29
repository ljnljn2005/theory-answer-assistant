from __future__ import annotations

import queue
import threading
from pathlib import Path
from tkinter import BooleanVar, END, StringVar, Tk, messagebox
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from automation import AutomationManager
from consensus import ProviderAnswer, compute_consensus, format_options
from providers import PROVIDERS


class AutomationThread:
    def __init__(self, workspace_root: Path, event_queue: queue.Queue[tuple[str, object]]) -> None:
        self.workspace_root = workspace_root
        self.event_queue = event_queue
        self.command_queue: queue.Queue[tuple[str, dict[str, object] | None]] = queue.Queue()
        self.thread = threading.Thread(target=self._run, name="automation-thread", daemon=True)
        self.thread.start()

    def submit(self, action: str, payload: dict[str, object] | None = None) -> None:
        self.command_queue.put((action, payload))

    def shutdown(self, join_timeout: float = 2.0) -> None:
        self.command_queue.put(("shutdown", None))
        self.thread.join(timeout=join_timeout)

    def _run(self) -> None:
        manager: AutomationManager | None = None
        manager_config: tuple[str, bool] | None = None

        while True:
            action, payload = self.command_queue.get()
            try:
                if action == "shutdown":
                    if manager is not None:
                        manager.close()
                    return

                if action == "close":
                    if manager is not None:
                        manager.close()
                        manager = None
                        manager_config = None
                    self.event_queue.put(("sessions_closed", None))
                    self.event_queue.put(("status", "浏览器会话已关闭"))
                    continue

                if payload is None:
                    raise RuntimeError("自动化线程收到空任务配置。")

                config = payload["config"]
                if not isinstance(config, tuple) or len(config) != 2:
                    raise RuntimeError("浏览器配置无效。")

                channel, headless = config
                if not isinstance(channel, str) or not isinstance(headless, bool):
                    raise RuntimeError("浏览器配置类型不正确。")

                if manager is None or manager_config != config:
                    if manager is not None:
                        manager.close()
                    manager = AutomationManager(
                        workspace_root=self.workspace_root,
                        channel=channel,
                        headless=headless,
                        status_callback=self._queue_provider_event,
                    )
                    manager_config = config

                if action == "warm_up":
                    manager.warm_up(PROVIDERS)
                    self.event_queue.put(("status", "网页会话已经打开，可以在浏览器中完成登录。"))
                elif action == "ask_all":
                    question = payload["question"]
                    timeout_seconds = payload["timeout_seconds"]
                    if not isinstance(question, str) or not isinstance(timeout_seconds, int):
                        raise RuntimeError("提问参数不正确。")
                    if headless:
                        self.event_queue.put(("status", "无头模式并发提问中..."))
                    results = manager.ask_all(PROVIDERS, question, timeout_seconds)
                    self.event_queue.put(("results", results))
                    self.event_queue.put(("status", "批量提问完成"))
                else:
                    raise RuntimeError(f"未知自动化动作: {action}")
            except Exception as exc:
                self.event_queue.put(("error", str(exc)))
            finally:
                if action != "shutdown":
                    self.event_queue.put(("task_finished", action))

    def _queue_provider_event(self, provider_key: str, status: str, detail: str) -> None:
        self.event_queue.put(("provider", (provider_key, status, detail)))


class AnswerAssistantApp:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.root.title("理论题作答助手")
        self.root.geometry("1320x860")
        self.root.minsize(1160, 760)

        self.workspace_root = Path(__file__).resolve().parent
        self.event_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.automation = AutomationThread(self.workspace_root, self.event_queue)
        self.task_in_flight = False

        self.browser_channel = StringVar(value="msedge")
        self.show_browser = BooleanVar(value=True)
        self.timeout_text = StringVar(value="90")
        self.status_text = StringVar(value="准备就绪")

        self.provider_rows: dict[str, str] = {}
        self._build_ui()
        self._populate_provider_rows()
        self.root.after(200, self._drain_events)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        main = ttk.Frame(self.root, padding=16)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(2, weight=1)
        main.rowconfigure(3, weight=1)

        question_frame = ttk.LabelFrame(main, text="题目与选项", padding=12)
        question_frame.grid(row=0, column=0, sticky="nsew")
        question_frame.columnconfigure(0, weight=1)

        self.question_text = ScrolledText(
            question_frame,
            height=9,
            wrap="word",
            font=("Microsoft YaHei UI", 11),
        )
        self.question_text.grid(row=0, column=0, sticky="nsew")
        self.question_text.insert(
            "1.0",
            "示例：\n"
            "下列关于计算机网络的说法，正确的是：\n"
            "A. HTTP 属于传输层协议\n"
            "B. TCP 提供可靠传输\n"
            "C. IP 负责进程到进程通信\n"
            "D. UDP 一定比 TCP 更安全",
        )

        control_frame = ttk.Frame(main, padding=(0, 12, 0, 12))
        control_frame.grid(row=1, column=0, sticky="ew")
        control_frame.columnconfigure(8, weight=1)

        ttk.Button(control_frame, text="打开/初始化网页", command=self.open_sessions).grid(
            row=0, column=0, padx=(0, 8)
        )
        ttk.Button(control_frame, text="批量提问", command=self.ask_all).grid(
            row=0, column=1, padx=(0, 8)
        )
        ttk.Button(control_frame, text="关闭浏览器会话", command=self.close_sessions).grid(
            row=0, column=2, padx=(0, 20)
        )

        ttk.Label(control_frame, text="浏览器").grid(row=0, column=3, sticky="w")
        browser_box = ttk.Combobox(
            control_frame,
            textvariable=self.browser_channel,
            values=("msedge", "chrome", "chromium"),
            width=12,
            state="readonly",
        )
        browser_box.grid(row=0, column=4, padx=(8, 16))

        ttk.Checkbutton(control_frame, text="显示浏览器窗口", variable=self.show_browser).grid(
            row=0, column=5, padx=(0, 16)
        )
        ttk.Label(control_frame, text="单站超时(秒)").grid(row=0, column=6, sticky="w")
        ttk.Entry(control_frame, textvariable=self.timeout_text, width=8).grid(
            row=0, column=7, padx=(8, 0)
        )
        ttk.Label(control_frame, textvariable=self.status_text, foreground="#3457d5").grid(
            row=0, column=8, sticky="e"
        )

        provider_frame = ttk.LabelFrame(main, text="平台状态", padding=12)
        provider_frame.grid(row=2, column=0, sticky="nsew", pady=(0, 12))
        provider_frame.columnconfigure(0, weight=1)
        provider_frame.rowconfigure(0, weight=1)

        columns = ("provider", "status", "option", "detail")
        self.provider_tree = ttk.Treeview(provider_frame, columns=columns, show="headings", height=8)
        self.provider_tree.heading("provider", text="平台")
        self.provider_tree.heading("status", text="状态")
        self.provider_tree.heading("option", text="识别选项")
        self.provider_tree.heading("detail", text="说明")
        self.provider_tree.column("provider", width=120, anchor="center")
        self.provider_tree.column("status", width=120, anchor="center")
        self.provider_tree.column("option", width=120, anchor="center")
        self.provider_tree.column("detail", width=720, anchor="w")
        self.provider_tree.grid(row=0, column=0, sticky="nsew")

        summary_frame = ttk.Frame(main)
        summary_frame.grid(row=3, column=0, sticky="nsew")
        summary_frame.columnconfigure(0, weight=1)
        summary_frame.columnconfigure(1, weight=1)

        result_box = ttk.LabelFrame(summary_frame, text="汇总结果", padding=12)
        result_box.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        result_box.columnconfigure(0, weight=1)
        result_box.rowconfigure(0, weight=1)

        self.result_summary = ScrolledText(
            result_box,
            height=12,
            wrap="word",
            font=("Microsoft YaHei UI", 11),
        )
        self.result_summary.grid(row=0, column=0, sticky="nsew")
        self.result_summary.insert("1.0", "这里会显示重合度最高的推荐答案。")
        self.result_summary.configure(state="disabled")

        log_box = ttk.LabelFrame(summary_frame, text="运行日志 / 原始回答", padding=12)
        log_box.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        log_box.columnconfigure(0, weight=1)
        log_box.rowconfigure(0, weight=1)

        self.log_text = ScrolledText(log_box, height=12, wrap="word", font=("Consolas", 10))
        self.log_text.grid(row=0, column=0, sticky="nsew")
        self.log_text.insert(
            "1.0",
            "首次使用请先点“打开/初始化网页”，在弹出的浏览器中完成各平台登录。",
        )
        self.log_text.configure(state="disabled")

    def _populate_provider_rows(self) -> None:
        for provider in PROVIDERS:
            row_id = self.provider_tree.insert("", END, values=(provider.name, "未启动", "-", ""))
            self.provider_rows[provider.key] = row_id

    def open_sessions(self) -> None:
        if self._guard_busy():
            return
        self.task_in_flight = True
        self.status_text.set("正在初始化网页会话...")
        self.automation.submit("warm_up", {"config": self._browser_config()})

    def ask_all(self) -> None:
        if self._guard_busy():
            return

        question = self.question_text.get("1.0", END).strip()
        if not question:
            messagebox.showwarning("提示", "请先输入题目。")
            return

        try:
            timeout_seconds = self._timeout_seconds()
        except RuntimeError as exc:
            messagebox.showerror("参数错误", str(exc))
            return

        self.task_in_flight = True
        self.status_text.set("正在批量提问...")
        self.automation.submit(
            "ask_all",
            {
                "config": self._browser_config(),
                "question": question,
                "timeout_seconds": timeout_seconds,
            },
        )

    def close_sessions(self) -> None:
        if self._guard_busy():
            return

        self.task_in_flight = True
        self.status_text.set("正在关闭浏览器会话...")
        self.automation.submit("close")

    def _guard_busy(self) -> bool:
        if self.task_in_flight:
            messagebox.showinfo("请稍候", "当前还有任务在运行，请稍等一下。")
            return True
        return False

    def _browser_config(self) -> tuple[str, bool]:
        return (self.browser_channel.get(), not self.show_browser.get())

    def _drain_events(self) -> None:
        while not self.event_queue.empty():
            event_type, payload = self.event_queue.get()
            if event_type == "provider":
                provider_key, status, detail = payload  # type: ignore[misc]
                self._handle_provider_event(provider_key, status, detail)
            elif event_type == "results":
                self._render_results(payload)  # type: ignore[arg-type]
            elif event_type == "sessions_closed":
                self._mark_sessions_closed()
            elif event_type == "status":
                self.status_text.set(str(payload))
                self._append_log(str(payload))
            elif event_type == "error":
                self.status_text.set("发生错误")
                self._append_log(f"[错误] {payload}")
                messagebox.showerror("运行失败", str(payload))
            elif event_type == "task_finished":
                self.task_in_flight = False
        self.root.after(200, self._drain_events)

    def _handle_provider_event(self, provider_key: str, status: str, detail: str) -> None:
        row_id = self.provider_rows[provider_key]
        provider_name = self.provider_tree.set(row_id, "provider")
        current_option = self.provider_tree.set(row_id, "option") or "-"
        self.provider_tree.item(row_id, values=(provider_name, status, current_option, detail))
        self._append_log(f"[{provider_name}] {status} - {detail}")

    def _render_results(self, results: list[ProviderAnswer]) -> None:
        consensus = compute_consensus(results)

        for item in results:
            option_text = format_options(item.parsed_options)
            detail = item.error or (item.raw_answer[:120].replace("\n", " ").strip() if item.raw_answer else "")
            status = "失败" if item.error else "完成"
            self._update_provider_row(item.provider_key, status, option_text, detail)

        lines: list[str] = []
        if consensus.recommended_options:
            exact_ratio = f"{consensus.exact_match_count}/{consensus.parsed_provider_count}"
            lines.append(f"推荐答案：{format_options(consensus.recommended_options)}")
            lines.append(f"精确重合：{exact_ratio}")
            if consensus.option_support:
                single_support = "  ".join(
                    f"{option}({count})" for option, count in consensus.option_support.items()
                )
                lines.append(f"单项支持：{single_support}")
            if consensus.exact_support:
                exact_support = "  ".join(
                    f"{option_set or '-'}({count})" for option_set, count in consensus.exact_support.items()
                )
                lines.append(f"组合分布：{exact_support}")
        else:
            lines.append("没有从回答中稳定识别出选项。")
            lines.append("建议先在浏览器里确认各平台都已登录，并观察是否按照“答案：A”格式返回。")

        lines.append("")
        lines.append("各平台原始回答：")
        for item in results:
            lines.append(f"[{item.provider_name}]")
            if item.error:
                lines.append(f"失败：{item.error}")
            else:
                lines.append(item.raw_answer.strip() or "(空回答)")
            lines.append("")

        self._set_text(self.result_summary, "\n".join(lines).strip())

    def _mark_sessions_closed(self) -> None:
        for provider in PROVIDERS:
            self._update_provider_row(provider.key, "已关闭", "-", "")

    def _update_provider_row(self, provider_key: str, status: str, option: str, detail: str) -> None:
        row_id = self.provider_rows[provider_key]
        provider_name = self.provider_tree.set(row_id, "provider")
        self.provider_tree.item(row_id, values=(provider_name, status, option, detail))

    def _set_text(self, widget: ScrolledText, content: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", END)
        widget.insert("1.0", content)
        widget.configure(state="disabled")

    def _append_log(self, line: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert(END, f"{line}\n")
        self.log_text.see(END)
        self.log_text.configure(state="disabled")

    def _timeout_seconds(self) -> int:
        value = self.timeout_text.get().strip()
        try:
            timeout = int(value)
        except ValueError as exc:
            raise RuntimeError("单站超时必须是整数秒。") from exc

        if timeout < 20:
            raise RuntimeError("单站超时建议至少 20 秒。")
        return timeout

    def _on_close(self) -> None:
        try:
            self.automation.shutdown()
        finally:
            self.root.destroy()


def main() -> None:
    root = Tk()
    style = ttk.Style()
    try:
        style.theme_use("vista")
    except Exception:
        pass
    AnswerAssistantApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
