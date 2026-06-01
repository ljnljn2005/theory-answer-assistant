from __future__ import annotations

from dataclasses import dataclass, field
import queue
import re
import threading
from pathlib import Path
from tkinter import BooleanVar, END, StringVar, Tk, messagebox
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from automation import AutomationManager, TaskCancelledError
from consensus import ProviderAnswer, compute_consensus, format_options
from providers import PROVIDERS, ProviderConfig


ACTIVE_TASK_STATUSES = {"待执行", "运行中", "终止中"}
QUESTION_SPLIT_RE = re.compile(r"(?m)^\s*(?:-{3,}|={3,}|_{3,})\s*$")


@dataclass(slots=True)
class QueueTask:
    task_id: str
    question: str
    title: str
    providers: tuple[ProviderConfig, ...]
    timeout_seconds: int
    browser_config: tuple[str, bool]
    status: str = "待执行"
    recommendation: str = "-"
    exact_ratio: str = "-"
    result_text: str = "等待执行..."
    log_lines: list[str] = field(default_factory=list)
    provider_states: dict[str, tuple[str, str, str]] = field(default_factory=dict)


class AutomationThread:
    def __init__(self, workspace_root: Path, event_queue: queue.Queue[tuple[str, object]]) -> None:
        self.workspace_root = workspace_root
        self.event_queue = event_queue
        self.command_queue: queue.Queue[tuple[str, dict[str, object] | None]] = queue.Queue()
        self.stop_event = threading.Event()
        self.cancelled_task_ids: set[str] = set()
        self.cancel_lock = threading.Lock()
        self.current_task_id: str | None = None
        self.thread = threading.Thread(target=self._run, name="automation-thread", daemon=True)
        self.thread.start()

    def submit(self, action: str, payload: dict[str, object] | None = None) -> None:
        self.command_queue.put((action, payload))

    def force_stop(self, task_ids: tuple[str, ...]) -> None:
        if not task_ids:
            return
        with self.cancel_lock:
            self.cancelled_task_ids.update(task_ids)
        self.stop_event.set()

    def shutdown(self, join_timeout: float = 2.0) -> None:
        self.stop_event.set()
        self.command_queue.put(("shutdown", None))
        self.thread.join(timeout=join_timeout)

    def _run(self) -> None:
        manager: AutomationManager | None = None
        manager_config: tuple[str, bool] | None = None

        while True:
            action, payload = self.command_queue.get()
            task_id: str | None = None
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
                    raise RuntimeError("自动化线程收到了空任务配置。")

                config = payload["config"]
                if not isinstance(config, tuple) or len(config) != 2:
                    raise RuntimeError("浏览器配置无效。")

                channel, headless = config
                if not isinstance(channel, str) or not isinstance(headless, bool):
                    raise RuntimeError("浏览器配置类型不正确。")

                providers = payload.get("providers", PROVIDERS)
                if not isinstance(providers, tuple) or not all(
                    isinstance(provider, ProviderConfig) for provider in providers
                ):
                    raise RuntimeError("平台配置无效。")

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
                    self.stop_event.clear()
                    manager.warm_up(providers, stop_event=self.stop_event)
                    self.event_queue.put(("status", "网页会话已经打开，可以在浏览器中完成登录。"))
                    continue

                if action != "ask_all":
                    raise RuntimeError(f"未知自动化动作: {action}")

                raw_task_id = payload.get("task_id")
                question = payload.get("question")
                timeout_seconds = payload.get("timeout_seconds")
                if not isinstance(raw_task_id, str) or not isinstance(question, str) or not isinstance(
                    timeout_seconds, int
                ):
                    raise RuntimeError("提问参数不正确。")

                task_id = raw_task_id
                if self._is_cancelled(task_id):
                    self.event_queue.put(("task_cancelled", {"task_id": task_id, "reason": "任务已从队列移除"}))
                    continue

                self.current_task_id = task_id
                self.stop_event.clear()
                self.event_queue.put(("task_started", task_id))
                if headless and len(providers) > 1:
                    self.event_queue.put(("status", f"{task_id} 无头模式并发提问中..."))

                results = manager.ask_all(
                    providers,
                    question,
                    timeout_seconds,
                    stop_event=self.stop_event,
                )
                self.event_queue.put(("results", {"task_id": task_id, "results": results}))
                self.event_queue.put(("status", f"{task_id} 处理完成"))
            except TaskCancelledError as exc:
                if task_id is not None:
                    self.event_queue.put(
                        ("task_cancelled", {"task_id": task_id, "reason": str(exc) or "任务已被强制结束"})
                    )
                else:
                    self.event_queue.put(("status", "当前操作已被强制结束"))
            except Exception as exc:
                if task_id is not None and (self.stop_event.is_set() or self._is_cancelled(task_id)):
                    self.event_queue.put(("task_cancelled", {"task_id": task_id, "reason": "任务已被强制结束"}))
                elif task_id is not None:
                    self.event_queue.put(("task_error", {"task_id": task_id, "message": str(exc)}))
                else:
                    self.event_queue.put(("error", str(exc)))
            finally:
                if task_id is not None:
                    self.current_task_id = None
                    self.stop_event.clear()
                    self._clear_cancelled(task_id)
                if action != "shutdown":
                    self.event_queue.put(("task_finished", {"action": action, "task_id": task_id}))

    def _queue_provider_event(self, provider_key: str, status: str, detail: str) -> None:
        self.event_queue.put(
            (
                "provider",
                {
                    "task_id": self.current_task_id,
                    "provider_key": provider_key,
                    "status": status,
                    "detail": detail,
                },
            )
        )

    def _is_cancelled(self, task_id: str) -> bool:
        with self.cancel_lock:
            return task_id in self.cancelled_task_ids

    def _clear_cancelled(self, task_id: str) -> None:
        with self.cancel_lock:
            self.cancelled_task_ids.discard(task_id)


class AnswerAssistantApp:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.root.title("理论题作答助手")
        self.root.geometry("1400x980")
        self.root.minsize(1220, 840)

        self.workspace_root = Path(__file__).resolve().parent
        self.event_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.automation = AutomationThread(self.workspace_root, self.event_queue)

        self.browser_channel = StringVar(value="msedge")
        self.show_browser = BooleanVar(value=True)
        self.timeout_text = StringVar(value="90")
        self.status_text = StringVar(value="准备就绪")
        self.queue_text = StringVar(value="队列：0 个任务")
        self.provider_vars: dict[str, BooleanVar] = {
            provider.key: BooleanVar(value=True) for provider in PROVIDERS
        }

        self.provider_rows: dict[str, str] = {}
        self.queue_rows: dict[str, str] = {}
        self.tasks: dict[str, QueueTask] = {}
        self.task_counter = 0
        self.selected_task_id: str | None = None
        self.running_task_id: str | None = None
        self.control_action: str | None = None
        self.system_log_lines = [
            "首次使用请先点“打开/初始化网页”，在弹出的浏览器中完成各平台登录。",
            "多个题目可以逐个加入队列，也可以在输入框中用 --- 分隔后一次性加入。",
        ]

        self._build_ui()
        self._populate_provider_rows()
        self._refresh_provider_selection()
        self._render_overview()
        self.root.after(200, self._drain_events)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        main = ttk.Frame(self.root, padding=16)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(3, weight=1)
        main.rowconfigure(4, weight=1)
        main.rowconfigure(5, weight=2)

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
            "D. UDP 一定比 TCP 更安全\n"
            "\n"
            "---\n"
            "用 --- 分隔后，可以一次性把多道题加入队列。",
        )

        control_frame = ttk.Frame(main, padding=(0, 12, 0, 12))
        control_frame.grid(row=1, column=0, sticky="ew")
        control_frame.columnconfigure(10, weight=1)

        ttk.Button(control_frame, text="打开/初始化网页", command=self.open_sessions).grid(
            row=0, column=0, padx=(0, 8)
        )
        ttk.Button(control_frame, text="加入队列", command=self.enqueue_questions).grid(
            row=0, column=1, padx=(0, 8)
        )
        ttk.Button(control_frame, text="强制结束", command=self.force_stop).grid(
            row=0, column=2, padx=(0, 8)
        )
        ttk.Button(control_frame, text="关闭浏览器会话", command=self.close_sessions).grid(
            row=0, column=3, padx=(0, 20)
        )

        ttk.Label(control_frame, text="浏览器").grid(row=0, column=4, sticky="w")
        browser_box = ttk.Combobox(
            control_frame,
            textvariable=self.browser_channel,
            values=("msedge", "chrome", "chromium"),
            width=12,
            state="readonly",
        )
        browser_box.grid(row=0, column=5, padx=(8, 16))

        ttk.Checkbutton(control_frame, text="显示浏览器窗口", variable=self.show_browser).grid(
            row=0, column=6, padx=(0, 16)
        )
        ttk.Label(control_frame, text="单站超时(秒)").grid(row=0, column=7, sticky="w")
        ttk.Entry(control_frame, textvariable=self.timeout_text, width=8).grid(
            row=0, column=8, padx=(8, 16)
        )
        ttk.Label(control_frame, textvariable=self.status_text, foreground="#3457d5").grid(
            row=0, column=10, sticky="e"
        )
        ttk.Label(control_frame, textvariable=self.queue_text, foreground="#666666").grid(
            row=1, column=0, columnspan=11, sticky="w", pady=(8, 0)
        )

        selection_frame = ttk.LabelFrame(main, text="使用平台", padding=12)
        selection_frame.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        for column in range(3):
            selection_frame.columnconfigure(column, weight=1)

        for index, provider in enumerate(PROVIDERS):
            row = index // 3
            column = index % 3
            check = ttk.Checkbutton(
                selection_frame,
                text=provider.name,
                variable=self.provider_vars[provider.key],
                command=self._on_provider_toggle,
            )
            check.grid(row=row, column=column, sticky="w", padx=(0, 16), pady=(0, 6))

        queue_frame = ttk.LabelFrame(main, text="任务队列", padding=12)
        queue_frame.grid(row=3, column=0, sticky="nsew", pady=(0, 12))
        queue_frame.columnconfigure(0, weight=1)
        queue_frame.rowconfigure(0, weight=1)

        queue_columns = ("task", "status", "answer", "providers", "title")
        self.queue_tree = ttk.Treeview(queue_frame, columns=queue_columns, show="headings", height=7)
        self.queue_tree.heading("task", text="任务")
        self.queue_tree.heading("status", text="状态")
        self.queue_tree.heading("answer", text="推荐答案")
        self.queue_tree.heading("providers", text="平台")
        self.queue_tree.heading("title", text="题目摘要")
        self.queue_tree.column("task", width=90, anchor="center")
        self.queue_tree.column("status", width=110, anchor="center")
        self.queue_tree.column("answer", width=120, anchor="center")
        self.queue_tree.column("providers", width=260, anchor="w")
        self.queue_tree.column("title", width=720, anchor="w")
        self.queue_tree.grid(row=0, column=0, sticky="nsew")
        self.queue_tree.bind("<<TreeviewSelect>>", self._on_queue_select)

        provider_frame = ttk.LabelFrame(main, text="当前任务平台状态", padding=12)
        provider_frame.grid(row=4, column=0, sticky="nsew", pady=(0, 12))
        provider_frame.columnconfigure(0, weight=1)
        provider_frame.rowconfigure(0, weight=1)

        provider_columns = ("provider", "status", "option", "detail")
        self.provider_tree = ttk.Treeview(
            provider_frame,
            columns=provider_columns,
            show="headings",
            height=6,
        )
        self.provider_tree.heading("provider", text="平台")
        self.provider_tree.heading("status", text="状态")
        self.provider_tree.heading("option", text="识别选项")
        self.provider_tree.heading("detail", text="说明")
        self.provider_tree.column("provider", width=120, anchor="center")
        self.provider_tree.column("status", width=120, anchor="center")
        self.provider_tree.column("option", width=120, anchor="center")
        self.provider_tree.column("detail", width=820, anchor="w")
        self.provider_tree.grid(row=0, column=0, sticky="nsew")

        summary_frame = ttk.Frame(main)
        summary_frame.grid(row=5, column=0, sticky="nsew")
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
        self.result_summary.configure(state="disabled")

        log_box = ttk.LabelFrame(summary_frame, text="运行日志 / 原始回答", padding=12)
        log_box.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        log_box.columnconfigure(0, weight=1)
        log_box.rowconfigure(0, weight=1)

        self.log_text = ScrolledText(log_box, height=12, wrap="word", font=("Consolas", 10))
        self.log_text.grid(row=0, column=0, sticky="nsew")
        self.log_text.configure(state="disabled")

    def _populate_provider_rows(self) -> None:
        for provider in PROVIDERS:
            row_id = self.provider_tree.insert("", END, values=(provider.name, "未启动", "-", ""))
            self.provider_rows[provider.key] = row_id

    def open_sessions(self) -> None:
        if self._guard_control_action("打开网页会话"):
            return

        providers = self._selected_providers()
        if not providers:
            messagebox.showwarning("提示", "请至少勾选一个平台。")
            return

        self.control_action = "warm_up"
        self.status_text.set("正在初始化网页会话...")
        self._append_system_log("开始初始化所选平台的网页会话。")
        self.automation.submit("warm_up", {"config": self._browser_config(), "providers": providers})

    def enqueue_questions(self) -> None:
        raw_text = self.question_text.get("1.0", END).strip()
        if not raw_text:
            messagebox.showwarning("提示", "请先输入题目。")
            return

        try:
            timeout_seconds = self._timeout_seconds()
        except RuntimeError as exc:
            messagebox.showerror("参数错误", str(exc))
            return

        providers = self._selected_providers()
        if not providers:
            messagebox.showwarning("提示", "请至少勾选一个平台。")
            return

        questions = self._split_questions(raw_text)
        created_task_ids: list[str] = []
        for question in questions:
            task = self._create_task(question, providers, timeout_seconds, self._browser_config())
            self.tasks[task.task_id] = task
            row_id = self.queue_tree.insert("", END, values=self._queue_row_values(task))
            self.queue_rows[task.task_id] = row_id
            self.automation.submit(
                "ask_all",
                {
                    "config": task.browser_config,
                    "providers": task.providers,
                    "question": task.question,
                    "timeout_seconds": task.timeout_seconds,
                    "task_id": task.task_id,
                },
            )
            created_task_ids.append(task.task_id)

        if created_task_ids:
            self._select_task(created_task_ids[0])
        self.status_text.set(f"已加入队列：{len(created_task_ids)} 题")
        self._append_system_log(f"已加入队列：{len(created_task_ids)} 题。")
        self._refresh_queue_summary()

    def force_stop(self) -> None:
        active_task_ids = tuple(
            task_id
            for task_id, task in self.tasks.items()
            if task.status in ACTIVE_TASK_STATUSES
        )
        if not active_task_ids:
            messagebox.showinfo("提示", "当前没有可强制结束的任务。")
            return

        for task_id in active_task_ids:
            task = self.tasks[task_id]
            if task_id == self.running_task_id:
                task.status = "终止中"
                self._append_task_log(task_id, "[系统] 正在强制结束当前任务。")
            elif task.status == "待执行":
                task.status = "已终止"
                task.result_text = "任务已从队列中移除。"
                self._append_task_log(task_id, "[系统] 任务已从队列中移除。")
            self._update_queue_row(task_id)

        self.automation.force_stop(active_task_ids)
        self.status_text.set("正在强制结束当前任务并清空待执行队列...")
        self._append_system_log("已请求强制结束当前任务，并清空待执行队列。")
        self._refresh_queue_summary()
        self._render_selected_task_views()

    def close_sessions(self) -> None:
        if self._guard_control_action("关闭浏览器会话"):
            return

        self.control_action = "close"
        self.status_text.set("正在关闭浏览器会话...")
        self._append_system_log("开始关闭浏览器会话。")
        self.automation.submit("close")

    def _guard_control_action(self, action_name: str) -> bool:
        if self.control_action is not None:
            messagebox.showinfo("请稍候", "当前还有浏览器控制任务在运行，请稍等一下。")
            return True
        if self._has_active_tasks():
            messagebox.showinfo("请先处理队列", f"队列中还有任务，先等待完成或点“强制结束”后再{action_name}。")
            return True
        return False

    def _browser_config(self) -> tuple[str, bool]:
        return (self.browser_channel.get(), not self.show_browser.get())

    def _drain_events(self) -> None:
        while not self.event_queue.empty():
            event_type, payload = self.event_queue.get()
            if event_type == "provider":
                self._handle_provider_event(payload)  # type: ignore[arg-type]
            elif event_type == "results":
                self._handle_task_results(payload)  # type: ignore[arg-type]
            elif event_type == "task_started":
                self._handle_task_started(str(payload))
            elif event_type == "task_cancelled":
                self._handle_task_cancelled(payload)  # type: ignore[arg-type]
            elif event_type == "task_error":
                self._handle_task_error(payload)  # type: ignore[arg-type]
            elif event_type == "sessions_closed":
                self._mark_sessions_closed()
            elif event_type == "status":
                self.status_text.set(str(payload))
                self._append_system_log(str(payload))
            elif event_type == "error":
                self.status_text.set("发生错误")
                self._append_system_log(f"[错误] {payload}")
                messagebox.showerror("运行失败", str(payload))
            elif event_type == "task_finished":
                self._handle_task_finished(payload)  # type: ignore[arg-type]
        self.root.after(200, self._drain_events)

    def _handle_provider_event(self, payload: dict[str, object]) -> None:
        task_id = payload.get("task_id")
        provider_key = payload.get("provider_key")
        status = payload.get("status")
        detail = payload.get("detail")
        if not isinstance(provider_key, str) or not isinstance(status, str) or not isinstance(detail, str):
            return

        if not isinstance(task_id, str):
            self._update_provider_row(provider_key, status, "-", detail)
            self._append_system_log(f"[{self._provider_name(provider_key)}] {status} - {detail}")
            return

        task = self.tasks.get(task_id)
        if task is None:
            return

        current_option = task.provider_states.get(provider_key, ("-", "-", ""))[1]
        task.provider_states[provider_key] = (status, current_option, detail)
        self._append_task_log(task_id, f"[{self._provider_name(provider_key)}] {status} - {detail}")

        if self.selected_task_id == task_id:
            self._render_task_provider_states(task)
            self._render_selected_task_views()

    def _handle_task_started(self, task_id: str) -> None:
        task = self.tasks.get(task_id)
        if task is None:
            return

        self.running_task_id = task_id
        task.status = "运行中"
        task.result_text = "正在执行，请稍候..."
        self._append_task_log(task_id, "[系统] 任务开始执行。")
        self._update_queue_row(task_id)
        self._refresh_queue_summary()
        self._select_task(task_id)
        self.status_text.set(f"{task_id} 运行中...")

    def _handle_task_results(self, payload: dict[str, object]) -> None:
        task_id = payload.get("task_id")
        results = payload.get("results")
        if not isinstance(task_id, str) or not isinstance(results, list):
            return
        typed_results = [item for item in results if isinstance(item, ProviderAnswer)]
        task = self.tasks.get(task_id)
        if task is None:
            return

        consensus = compute_consensus(typed_results)
        for item in typed_results:
            option_text = format_options(item.parsed_options)
            detail = item.error or (item.raw_answer[:120].replace("\n", " ").strip() if item.raw_answer else "")
            status = "失败" if item.error else "完成"
            task.provider_states[item.provider_key] = (status, option_text, detail)

        if consensus.recommended_options:
            task.recommendation = format_options(consensus.recommended_options)
            task.exact_ratio = f"{consensus.exact_match_count}/{consensus.parsed_provider_count}"
        else:
            task.recommendation = "-"
            task.exact_ratio = "-"

        task.status = "已完成" if typed_results and any(not item.error for item in typed_results) else "已失败"
        task.result_text = self._build_result_text(typed_results, consensus)
        self._update_queue_row(task_id)
        self._refresh_queue_summary()
        if self.selected_task_id == task_id:
            self._render_task_provider_states(task)
            self._render_selected_task_views()

    def _handle_task_cancelled(self, payload: dict[str, object]) -> None:
        task_id = payload.get("task_id")
        reason = payload.get("reason")
        if not isinstance(task_id, str):
            return
        task = self.tasks.get(task_id)
        if task is None:
            return

        task.status = "已终止"
        task.result_text = reason if isinstance(reason, str) and reason else "任务已被强制结束。"
        self._append_task_log(task_id, f"[系统] {task.result_text}")
        self._update_queue_row(task_id)
        self._refresh_queue_summary()
        if self.selected_task_id == task_id:
            self._render_selected_task_views()

    def _handle_task_error(self, payload: dict[str, object]) -> None:
        task_id = payload.get("task_id")
        message = payload.get("message")
        if not isinstance(task_id, str) or not isinstance(message, str):
            return
        task = self.tasks.get(task_id)
        if task is None:
            return

        task.status = "已失败"
        task.result_text = message
        self._append_task_log(task_id, f"[错误] {message}")
        self._update_queue_row(task_id)
        self._refresh_queue_summary()
        if self.selected_task_id == task_id:
            self._render_selected_task_views()

    def _handle_task_finished(self, payload: dict[str, object]) -> None:
        action = payload.get("action")
        task_id = payload.get("task_id")

        if action == "ask_all" and isinstance(task_id, str) and self.running_task_id == task_id:
            self.running_task_id = None
        elif action in {"warm_up", "close"}:
            self.control_action = None

        self._refresh_queue_summary()
        self._render_selected_task_views()

    def _mark_sessions_closed(self) -> None:
        if self.selected_task_id is not None:
            return
        for provider in PROVIDERS:
            if self.provider_vars[provider.key].get():
                self._update_provider_row(provider.key, "已关闭", "-", "")
            else:
                self._update_provider_row(provider.key, "未选择", "-", "本次不会使用该平台")

    def _queue_row_values(self, task: QueueTask) -> tuple[str, str, str, str, str]:
        answer_text = task.recommendation
        if task.recommendation != "-" and task.exact_ratio != "-":
            answer_text = f"{task.recommendation} [{task.exact_ratio}]"
        providers_text = " / ".join(provider.name for provider in task.providers)
        return (task.task_id, task.status, answer_text, providers_text, task.title)

    def _update_queue_row(self, task_id: str) -> None:
        task = self.tasks[task_id]
        row_id = self.queue_rows[task_id]
        self.queue_tree.item(row_id, values=self._queue_row_values(task))

    def _selected_providers(self) -> tuple[ProviderConfig, ...]:
        return tuple(provider for provider in PROVIDERS if self.provider_vars[provider.key].get())

    def _on_provider_toggle(self) -> None:
        if self.selected_task_id is None:
            self._refresh_provider_selection()

    def _on_queue_select(self, _event) -> None:
        selection = self.queue_tree.selection()
        if not selection:
            return
        row_id = selection[0]
        for task_id, stored_row_id in self.queue_rows.items():
            if stored_row_id == row_id:
                self.selected_task_id = task_id
                break
        self._render_selected_task_views()

    def _select_task(self, task_id: str) -> None:
        row_id = self.queue_rows.get(task_id)
        if row_id is None:
            return
        self.selected_task_id = task_id
        self.queue_tree.selection_set(row_id)
        self.queue_tree.focus(row_id)
        self.queue_tree.see(row_id)
        self._render_selected_task_views()

    def _render_selected_task_views(self) -> None:
        task = self.tasks.get(self.selected_task_id or "")
        if task is None:
            self._render_overview()
            self._refresh_provider_selection()
            return

        self._render_task_provider_states(task)
        self._set_text(self.result_summary, task.result_text)
        log_content = "\n".join(task.log_lines).strip() or "暂无日志。"
        self._set_text(self.log_text, log_content)

    def _render_task_provider_states(self, task: QueueTask) -> None:
        for provider in PROVIDERS:
            status, option, detail = task.provider_states.get(
                provider.key,
                ("未选择", "-", "本题不使用该平台"),
            )
            self._update_provider_row(provider.key, status, option, detail)

    def _render_overview(self) -> None:
        total = len(self.tasks)
        pending = sum(1 for task in self.tasks.values() if task.status == "待执行")
        running = sum(1 for task in self.tasks.values() if task.status in {"运行中", "终止中"})
        done = sum(1 for task in self.tasks.values() if task.status == "已完成")
        failed = sum(1 for task in self.tasks.values() if task.status == "已失败")
        stopped = sum(1 for task in self.tasks.values() if task.status == "已终止")
        overview_lines = [
            "这里会显示当前选中任务的汇总结果。",
            "",
            f"队列概览：总 {total} 题，待执行 {pending}，运行中 {running}，已完成 {done}，已失败 {failed}，已终止 {stopped}",
            "点击上方“任务队列”里的某一行，可以查看那道题的各平台回答和最终汇总。",
        ]
        self._set_text(self.result_summary, "\n".join(overview_lines))
        self._set_text(self.log_text, "\n".join(self.system_log_lines).strip())

    def _refresh_provider_selection(self) -> None:
        if self.selected_task_id is not None:
            return
        selected_keys = {provider.key for provider in self._selected_providers()}
        for provider in PROVIDERS:
            if provider.key in selected_keys:
                self._update_provider_row(provider.key, "未启动", "-", "")
            else:
                self._update_provider_row(provider.key, "未选择", "-", "本次不会使用该平台")

    def _refresh_queue_summary(self) -> None:
        total = len(self.tasks)
        pending = sum(1 for task in self.tasks.values() if task.status == "待执行")
        running = sum(1 for task in self.tasks.values() if task.status in {"运行中", "终止中"})
        done = sum(1 for task in self.tasks.values() if task.status == "已完成")
        failed = sum(1 for task in self.tasks.values() if task.status == "已失败")
        stopped = sum(1 for task in self.tasks.values() if task.status == "已终止")
        self.queue_text.set(
            f"队列：总 {total} 题 | 待执行 {pending} | 运行中 {running} | 已完成 {done} | 已失败 {failed} | 已终止 {stopped}"
        )

    def _has_active_tasks(self) -> bool:
        return any(task.status in ACTIVE_TASK_STATUSES for task in self.tasks.values())

    def _create_task(
        self,
        question: str,
        providers: tuple[ProviderConfig, ...],
        timeout_seconds: int,
        browser_config: tuple[str, bool],
    ) -> QueueTask:
        self.task_counter += 1
        task_id = f"Q{self.task_counter:03d}"
        title = self._question_title(question)
        selected_keys = {provider.key for provider in providers}
        provider_states = {
            provider.key: (
                "待执行" if provider.key in selected_keys else "未选择",
                "-",
                "" if provider.key in selected_keys else "本题不使用该平台",
            )
            for provider in PROVIDERS
        }
        return QueueTask(
            task_id=task_id,
            question=question,
            title=title,
            providers=providers,
            timeout_seconds=timeout_seconds,
            browser_config=browser_config,
            provider_states=provider_states,
            log_lines=["[系统] 任务已加入队列。"],
        )

    def _question_title(self, question: str) -> str:
        for line in question.splitlines():
            stripped = line.strip()
            if stripped:
                return stripped[:80]
        return "未命名题目"

    def _split_questions(self, raw_text: str) -> list[str]:
        normalized = raw_text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not normalized:
            return []
        parts = [part.strip() for part in QUESTION_SPLIT_RE.split(normalized) if part.strip()]
        return parts or [normalized]

    def _build_result_text(self, results: list[ProviderAnswer], consensus) -> str:
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
            lines.append("建议先确认各平台已登录，并观察是否按“答案：A”格式返回。")

        lines.append("")
        lines.append("各平台原始回答：")
        for item in results:
            lines.append(f"[{item.provider_name}]")
            if item.error:
                lines.append(f"失败：{item.error}")
            else:
                lines.append(item.raw_answer.strip() or "(空回答)")
            lines.append("")

        return "\n".join(lines).strip()

    def _provider_name(self, provider_key: str) -> str:
        for provider in PROVIDERS:
            if provider.key == provider_key:
                return provider.name
        return provider_key

    def _append_task_log(self, task_id: str, line: str) -> None:
        task = self.tasks.get(task_id)
        if task is None:
            return
        task.log_lines.append(line)

    def _append_system_log(self, line: str) -> None:
        self.system_log_lines.append(line)
        if self.selected_task_id is None:
            self._render_overview()

    def _update_provider_row(self, provider_key: str, status: str, option: str, detail: str) -> None:
        row_id = self.provider_rows[provider_key]
        provider_name = self.provider_tree.set(row_id, "provider")
        self.provider_tree.item(row_id, values=(provider_name, status, option, detail))

    def _set_text(self, widget: ScrolledText, content: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", END)
        widget.insert("1.0", content)
        widget.configure(state="disabled")

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
        active_task_ids = tuple(
            task_id
            for task_id, task in self.tasks.items()
            if task.status in ACTIVE_TASK_STATUSES
        )
        if active_task_ids:
            self.automation.force_stop(active_task_ids)
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
