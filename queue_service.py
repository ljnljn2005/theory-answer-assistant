from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
import queue
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from automation import AutomationManager, TaskCancelledError, ask_provider_isolated
from consensus import ProviderAnswer, compute_consensus, format_options
from providers import PROVIDERS, ProviderConfig


ACTIVE_TASK_STATUSES = {"待执行", "运行中", "终止中"}
RUNNING_PROVIDER_STATUSES = {
    "待执行",
    "等待平台",
    "运行中",
    "终止中",
    "opening",
    "launching",
    "typing",
    "submitting",
    "waiting_answer",
    "waiting_login",
    "navigating",
    "ready",
}
QUESTION_SPLIT_RE = re.compile(r"(?m)^\s*(?:-{3,}|={3,}|_{3,})\s*$")
MAX_LOG_LINES = 240


def _provider_name(provider_key: str) -> str:
    for provider in PROVIDERS:
        if provider.key == provider_key:
            return provider.name
    return provider_key


def _trim_lines(lines: list[str], limit: int = MAX_LOG_LINES) -> None:
    if len(lines) > limit:
        del lines[: len(lines) - limit]


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
    provider_results: dict[str, ProviderAnswer] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.updated_at = time.time()

    def provider_names(self) -> list[str]:
        return [provider.name for provider in self.providers]

    def is_headless(self) -> bool:
        return self.browser_config[1]

    def to_summary(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "status": self.status,
            "recommendation": self.recommendation,
            "exact_ratio": self.exact_ratio,
            "providers": self.provider_names(),
            "updated_at": self.updated_at,
            "headless": self.is_headless(),
        }

    def to_detail(self) -> dict[str, Any]:
        selected_keys = {provider.key for provider in self.providers}
        provider_rows = []
        for provider in PROVIDERS:
            status, option, detail = self.provider_states.get(
                provider.key,
                ("未选择", "-", "本题不使用该平台"),
            )
            provider_rows.append(
                {
                    "key": provider.key,
                    "name": provider.name,
                    "selected": provider.key in selected_keys,
                    "status": status,
                    "option": option,
                    "detail": detail,
                }
            )
        return {
            "task_id": self.task_id,
            "title": self.title,
            "question": self.question,
            "status": self.status,
            "recommendation": self.recommendation,
            "exact_ratio": self.exact_ratio,
            "result_text": self.result_text,
            "logs": list(self.log_lines),
            "providers": provider_rows,
        }


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
                        result_callback=self._queue_provider_result,
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

    def _queue_provider_result(self, result: ProviderAnswer) -> None:
        self.event_queue.put(
            (
                "provider_result",
                {
                    "task_id": self.current_task_id,
                    "result": result,
                },
            )
        )

    def _is_cancelled(self, task_id: str) -> bool:
        with self.cancel_lock:
            return task_id in self.cancelled_task_ids

    def _clear_cancelled(self, task_id: str) -> None:
        with self.cancel_lock:
            self.cancelled_task_ids.discard(task_id)


class AnswerQueueService:
    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root
        self.event_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.automation = AutomationThread(workspace_root, self.event_queue)
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.event_thread = threading.Thread(target=self._event_loop, name="queue-event-thread", daemon=True)

        self.tasks: dict[str, QueueTask] = {}
        self.task_order: list[str] = []
        self.task_counter = 0
        self.running_task_ids: set[str] = set()
        self.headless_task_threads: dict[str, threading.Thread] = {}
        self.task_stop_events: dict[str, threading.Event] = {}
        self.provider_locks = {provider.key: threading.Lock() for provider in PROVIDERS}

        self.control_action: str | None = None
        self.status_text = "准备就绪"
        self.max_parallel_tasks = 1
        self.system_log_lines = [
            "首次使用请先打开网页会话，再在弹出的浏览器中完成各平台登录。",
            "多个题目可以用单独一行 --- 分隔后一次性加入队列。",
            "取消勾选“显示浏览器窗口”后，可开启多题并发询问。",
        ]
        self.session_provider_states: dict[str, tuple[str, str, str]] = {
            provider.key: ("未启动", "-", "") for provider in PROVIDERS
        }
        self.last_selected_provider_keys = {provider.key for provider in PROVIDERS}
        self.event_thread.start()

    def warm_up(self, provider_keys: list[str], browser: str, show_browser: bool) -> None:
        providers = self._resolve_provider_keys(provider_keys)
        with self.lock:
            self._guard_control_action("打开网页会话")
            self._guard_no_active_tasks("打开网页会话")
            self.control_action = "warm_up"
            self.status_text = "正在初始化网页会话..."
            self.last_selected_provider_keys = {provider.key for provider in providers}
            self._set_session_provider_selection(self.last_selected_provider_keys, "待初始化")
            self._append_system_log("开始初始化所选平台的网页会话。")
        self.automation.submit(
            "warm_up",
            {
                "config": (browser, not show_browser),
                "providers": providers,
            },
        )

    def enqueue_questions(
        self,
        question_text: str,
        provider_keys: list[str],
        timeout_seconds: int,
        browser: str,
        show_browser: bool,
        max_parallel_tasks: int = 1,
    ) -> list[str]:
        providers = self._resolve_provider_keys(provider_keys)
        questions = self._split_questions(question_text)
        if not questions:
            raise RuntimeError("请先输入题目。")

        with self.lock:
            if self.control_action is not None:
                raise RuntimeError("当前还有浏览器控制任务在运行，请稍等一下。")

            self.last_selected_provider_keys = {provider.key for provider in providers}
            self.max_parallel_tasks = 1 if show_browser else max(1, max_parallel_tasks)

            created_task_ids: list[str] = []
            for question in questions:
                task = self._create_task(question, providers, timeout_seconds, (browser, not show_browser))
                self.tasks[task.task_id] = task
                self.task_order.append(task.task_id)
                created_task_ids.append(task.task_id)

            self.status_text = f"已加入队列：{len(created_task_ids)} 题"
            self._append_system_log(f"已加入队列：{len(created_task_ids)} 题。")
            self._dispatch_pending_tasks_locked()
            return created_task_ids

    def force_stop(self, task_ids: list[str] | None = None) -> list[str]:
        visible_running_ids: list[str] = []
        with self.lock:
            active_task_ids = [
                task_id
                for task_id in (task_ids or self.task_order)
                if task_id in self.tasks and self.tasks[task_id].status in ACTIVE_TASK_STATUSES
            ]
            if not active_task_ids:
                return []

            for task_id in active_task_ids:
                task = self.tasks[task_id]
                if task.status == "待执行":
                    task.status = "已终止"
                    task.result_text = "任务已从队列中移除。"
                    self._mark_task_providers_stopped(task, default_detail="任务已从队列中移除")
                    self._append_task_log(task_id, "[系统] 任务已从队列中移除。")
                else:
                    task.status = "终止中"
                    self._append_task_log(task_id, "[系统] 正在强制结束当前任务。")

                if task.is_headless():
                    self.task_stop_events.setdefault(task_id, threading.Event()).set()
                else:
                    visible_running_ids.append(task_id)
                task.touch()

            self.status_text = "正在强制结束当前任务并清空待执行队列..."
            self._append_system_log("已请求强制结束当前任务，并清空待执行队列。")

        if visible_running_ids:
            self.automation.force_stop(tuple(visible_running_ids))
        return active_task_ids

    def clear_queue(self, task_ids: list[str] | None = None) -> list[str]:
        with self.lock:
            removable_task_ids = [
                task_id
                for task_id in (task_ids or self.task_order)
                if task_id in self.tasks and task_id not in self.running_task_ids
            ]
            if not removable_task_ids:
                return []

            removed_task_ids = set(removable_task_ids)
            for task_id in removable_task_ids:
                self.tasks.pop(task_id, None)
                self.task_stop_events.pop(task_id, None)
                self.headless_task_threads.pop(task_id, None)

            self.task_order = [task_id for task_id in self.task_order if task_id not in removed_task_ids]
            self._append_system_log(f"已清空队列内容：{len(removable_task_ids)} 题。")
            self._refresh_status_text()
            return removable_task_ids

    def close_sessions(self) -> None:
        with self.lock:
            self._guard_control_action("关闭浏览器会话")
            self._guard_no_active_tasks("关闭浏览器会话")
            self.control_action = "close"
            self.status_text = "正在关闭浏览器会话..."
            self._append_system_log("开始关闭浏览器会话。")
        self.automation.submit("close")

    def get_snapshot(self, selected_task_id: str | None = None) -> dict[str, Any]:
        with self.lock:
            task_summaries = [self.tasks[task_id].to_summary() for task_id in self.task_order if task_id in self.tasks]
            active_count = sum(1 for task in self.tasks.values() if task.status in ACTIVE_TASK_STATUSES)
            queue_summary = {
                "total": len(self.tasks),
                "pending": sum(1 for task in self.tasks.values() if task.status == "待执行"),
                "running": sum(1 for task in self.tasks.values() if task.status in {"运行中", "终止中"}),
                "completed": sum(1 for task in self.tasks.values() if task.status == "已完成"),
                "failed": sum(1 for task in self.tasks.values() if task.status == "已失败"),
                "stopped": sum(1 for task in self.tasks.values() if task.status == "已终止"),
                "clearable": sum(
                    1 for task_id in self.task_order if task_id in self.tasks and task_id not in self.running_task_ids
                ),
            }

            selected_task = None
            if selected_task_id and selected_task_id in self.tasks:
                selected_task = self.tasks[selected_task_id].to_detail()

            provider_rows = []
            for provider in PROVIDERS:
                status, option, detail = self.session_provider_states.get(provider.key, ("未启动", "-", ""))
                provider_rows.append(
                    {
                        "key": provider.key,
                        "name": provider.name,
                        "selected": provider.key in self.last_selected_provider_keys,
                        "status": status,
                        "option": option,
                        "detail": detail,
                    }
                )

            return {
                "status_text": self.status_text,
                "control_action": self.control_action,
                "running_task_id": next(iter(sorted(self.running_task_ids))) if self.running_task_ids else None,
                "running_task_ids": sorted(self.running_task_ids),
                "has_active_tasks": active_count > 0,
                "queue_summary": queue_summary,
                "providers": [{"key": provider.key, "name": provider.name} for provider in PROVIDERS],
                "session_providers": provider_rows,
                "tasks": task_summaries,
                "selected_task": selected_task,
                "system_logs": list(self.system_log_lines[-MAX_LOG_LINES:]),
                "max_parallel_tasks": self.max_parallel_tasks,
            }

    def shutdown(self) -> None:
        active_task_ids: list[str]
        with self.lock:
            active_task_ids = [
                task_id for task_id, task in self.tasks.items() if task.status in ACTIVE_TASK_STATUSES
            ]
        if active_task_ids:
            self.force_stop(active_task_ids)
            for thread in list(self.headless_task_threads.values()):
                thread.join(timeout=2.0)
        self.automation.shutdown()
        self.stop_event.set()
        self.event_queue.put(("__service_shutdown__", None))
        self.event_thread.join(timeout=2.0)

    def _event_loop(self) -> None:
        while not self.stop_event.is_set():
            event_type, payload = self.event_queue.get()
            if event_type == "__service_shutdown__":
                return
            with self.lock:
                self._apply_event(event_type, payload)

    def _apply_event(self, event_type: str, payload: object) -> None:
        if event_type == "provider" and isinstance(payload, dict):
            self._handle_provider_event(payload)
        elif event_type == "provider_result" and isinstance(payload, dict):
            self._handle_provider_result_event(payload)
        elif event_type == "results" and isinstance(payload, dict):
            self._handle_task_results(payload)
        elif event_type == "task_started" and isinstance(payload, str):
            self._handle_task_started(payload)
        elif event_type == "task_cancelled" and isinstance(payload, dict):
            self._handle_task_cancelled(payload)
        elif event_type == "task_error" and isinstance(payload, dict):
            self._handle_task_error(payload)
        elif event_type == "sessions_closed":
            self._mark_sessions_closed()
        elif event_type == "status":
            self.status_text = str(payload)
            self._append_system_log(str(payload))
        elif event_type == "error":
            message = str(payload)
            self.status_text = "发生错误"
            self._append_system_log(f"[错误] {message}")
        elif event_type == "task_finished" and isinstance(payload, dict):
            self._handle_task_finished(payload)

    def _dispatch_pending_tasks_locked(self) -> None:
        if self.control_action is not None:
            return

        visible_running = any(
            task_id in self.tasks and not self.tasks[task_id].is_headless() for task_id in self.running_task_ids
        )
        headless_running_count = sum(
            1 for task_id in self.running_task_ids if task_id in self.tasks and self.tasks[task_id].is_headless()
        )

        for task_id in self.task_order:
            task = self.tasks.get(task_id)
            if task is None or task.status != "待执行":
                continue

            if not task.is_headless():
                if self.running_task_ids:
                    return
                self._start_visible_task_locked(task)
                return

            if visible_running:
                return
            if headless_running_count >= self.max_parallel_tasks:
                return

            self._start_headless_task_locked(task)
            headless_running_count += 1

    def _start_visible_task_locked(self, task: QueueTask) -> None:
        self.running_task_ids.add(task.task_id)
        self._handle_task_started(task.task_id)
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

    def _start_headless_task_locked(self, task: QueueTask) -> None:
        stop_event = threading.Event()
        thread = threading.Thread(
            target=self._run_headless_task,
            args=(task.task_id,),
            name=f"headless-task-{task.task_id}",
            daemon=True,
        )
        self.task_stop_events[task.task_id] = stop_event
        self.headless_task_threads[task.task_id] = thread
        self.running_task_ids.add(task.task_id)
        self._handle_task_started(task.task_id)
        thread.start()

    def _run_headless_task(self, task_id: str) -> None:
        try:
            with self.lock:
                task = self.tasks.get(task_id)
                if task is None:
                    return
                stop_event = self.task_stop_events.get(task_id)
                if stop_event is None:
                    stop_event = threading.Event()
                    self.task_stop_events[task_id] = stop_event
                providers = task.providers
                question = task.question
                timeout_seconds = task.timeout_seconds
                channel = task.browser_config[0]

            results = self._ask_headless_task_providers(
                task_id=task_id,
                providers=providers,
                question=question,
                timeout_seconds=timeout_seconds,
                channel=channel,
                stop_event=stop_event,
            )

            if stop_event.is_set():
                raise TaskCancelledError("任务已被强制结束")

            with self.lock:
                self._handle_task_results({"task_id": task_id, "results": results})
        except TaskCancelledError as exc:
            with self.lock:
                self._handle_task_cancelled({"task_id": task_id, "reason": str(exc) or "任务已被强制结束"})
        except Exception as exc:
            with self.lock:
                self._handle_task_error({"task_id": task_id, "message": str(exc)})
        finally:
            with self.lock:
                self.running_task_ids.discard(task_id)
                self.task_stop_events.pop(task_id, None)
                self.headless_task_threads.pop(task_id, None)
                self._refresh_status_text()
                self._dispatch_pending_tasks_locked()

    def _ask_headless_task_providers(
        self,
        task_id: str,
        providers: tuple[ProviderConfig, ...],
        question: str,
        timeout_seconds: int,
        channel: str,
        stop_event: threading.Event,
    ) -> list[ProviderAnswer]:
        results_by_key: dict[str, ProviderAnswer] = {}
        with ThreadPoolExecutor(max_workers=len(providers), thread_name_prefix=f"{task_id}-provider") as executor:
            future_map = {
                executor.submit(
                    self._run_headless_provider,
                    task_id,
                    provider,
                    question,
                    timeout_seconds,
                    channel,
                    stop_event,
                ): provider
                for provider in providers
            }
            for future in as_completed(future_map):
                provider = future_map[future]
                result = future.result()
                results_by_key[provider.key] = result
                with self.lock:
                    self._apply_provider_result_to_task(task_id, result)

        if stop_event.is_set() and not results_by_key:
            raise TaskCancelledError("任务已被强制结束")

        return [
            results_by_key.get(
                provider.key,
                ProviderAnswer(
                    provider_key=provider.key,
                    provider_name=provider.name,
                    error="任务已被强制结束",
                ),
            )
            for provider in providers
        ]

    def _run_headless_provider(
        self,
        task_id: str,
        provider: ProviderConfig,
        question: str,
        timeout_seconds: int,
        channel: str,
        stop_event: threading.Event,
    ) -> ProviderAnswer:
        isolated_profile_root = self._prepare_isolated_profile_root(provider.key)
        if isolated_profile_root is not None:
            try:
                return ask_provider_isolated(
                    workspace_root=self.workspace_root,
                    provider=provider,
                    question=question,
                    timeout_seconds=timeout_seconds,
                    channel=channel,
                    headless=True,
                    status_callback=self._headless_provider_callback(task_id),
                    stop_event=stop_event,
                    profile_root=isolated_profile_root,
                )
            finally:
                self._cleanup_isolated_profile_root(isolated_profile_root)

        self._set_provider_waiting_for_slot(task_id, provider.key)
        provider_lock = self.provider_locks[provider.key]
        acquired = False
        while not acquired:
            if stop_event.is_set():
                raise TaskCancelledError("任务已被强制结束")
            acquired = provider_lock.acquire(timeout=0.2)

        try:
            return ask_provider_isolated(
                workspace_root=self.workspace_root,
                provider=provider,
                question=question,
                timeout_seconds=timeout_seconds,
                channel=channel,
                headless=True,
                status_callback=self._headless_provider_callback(task_id),
                stop_event=stop_event,
            )
        finally:
            provider_lock.release()

    def _headless_provider_callback(self, task_id: str) -> Callable[[str, str, str], None]:
        def callback(provider_key: str, status: str, detail: str) -> None:
            with self.lock:
                self._handle_provider_event(
                    {
                        "task_id": task_id,
                        "provider_key": provider_key,
                        "status": status,
                        "detail": detail,
                    }
                )

        return callback

    def _prepare_isolated_profile_root(self, provider_key: str) -> Path | None:
        base_profile_dir = self.workspace_root / "profiles" / provider_key
        worker_root = self.workspace_root / "profiles" / "_workers"
        isolated_root = worker_root / f"{provider_key}-{uuid4().hex}"
        target_profile_dir = isolated_root / provider_key

        try:
            isolated_root.mkdir(parents=True, exist_ok=False)
            if base_profile_dir.exists():
                shutil.copytree(
                    base_profile_dir,
                    target_profile_dir,
                    ignore=shutil.ignore_patterns(
                        "Crashpad",
                        "CrashpadMetrics*",
                        "Singleton*",
                        "*.lock",
                        "lockfile",
                        "GPUCache",
                        "Code Cache",
                        "ShaderCache",
                        "GrShaderCache",
                        "DawnCache",
                        "BrowserMetrics*",
                    ),
                )
            else:
                target_profile_dir.mkdir(parents=True, exist_ok=True)
            return isolated_root
        except Exception:
            self._cleanup_isolated_profile_root(isolated_root)
            return None

    def _cleanup_isolated_profile_root(self, isolated_root: Path | None) -> None:
        if isolated_root is None or not isolated_root.exists():
            return
        for _ in range(5):
            try:
                shutil.rmtree(isolated_root, ignore_errors=False)
                return
            except Exception:
                time.sleep(0.2)
        shutil.rmtree(isolated_root, ignore_errors=True)

    def _set_provider_waiting_for_slot(self, task_id: str, provider_key: str) -> None:
        with self.lock:
            task = self.tasks.get(task_id)
            if task is None:
                return
            current_status, current_option, _ = task.provider_states.get(provider_key, ("待执行", "-", ""))
            if current_status == "待执行":
                task.provider_states[provider_key] = ("等待平台", current_option, "等待该平台空闲")
                task.touch()

    def _handle_provider_event(self, payload: dict[str, object]) -> None:
        task_id = payload.get("task_id")
        provider_key = payload.get("provider_key")
        status = payload.get("status")
        detail = payload.get("detail")
        if not isinstance(provider_key, str) or not isinstance(status, str) or not isinstance(detail, str):
            return

        if not isinstance(task_id, str):
            self.session_provider_states[provider_key] = (status, "-", detail)
            self._append_system_log(f"[{_provider_name(provider_key)}] {status} - {detail}")
            return

        task = self.tasks.get(task_id)
        if task is None:
            return

        current_option = task.provider_states.get(provider_key, ("-", "-", ""))[1]
        task.provider_states[provider_key] = (status, current_option, detail)
        task.touch()
        self._append_task_log(task_id, f"[{_provider_name(provider_key)}] {status} - {detail}")

    def _handle_provider_result_event(self, payload: dict[str, object]) -> None:
        task_id = payload.get("task_id")
        result = payload.get("result")
        if not isinstance(task_id, str) or not isinstance(result, ProviderAnswer):
            return
        self._apply_provider_result_to_task(task_id, result)

    def _apply_provider_result_to_task(self, task_id: str, result: ProviderAnswer) -> None:
        task = self.tasks.get(task_id)
        if task is None:
            return

        task.provider_results[result.provider_key] = result
        option_text = format_options(result.parsed_options)
        detail = result.error or (result.raw_answer[:120].replace("\n", " ").strip() if result.raw_answer else "")
        task.provider_states[result.provider_key] = ("失败" if result.error else "完成", option_text, detail)
        task.recommendation, task.exact_ratio, task.result_text = self._build_live_result(task)
        task.touch()
        self._append_task_log(
            task_id,
            f"[{result.provider_name}] {'失败' if result.error else '完成'} - {detail or option_text or '-'}",
        )

    def _build_live_result(self, task: QueueTask) -> tuple[str, str, str]:
        results = [
            task.provider_results[provider.key]
            for provider in task.providers
            if provider.key in task.provider_results
        ]
        consensus = compute_consensus(results)
        recommendation = format_options(consensus.recommended_options) if consensus.recommended_options else "-"
        exact_ratio = (
            f"{consensus.exact_match_count}/{consensus.parsed_provider_count}"
            if consensus.recommended_options
            else "-"
        )
        return recommendation, exact_ratio, self._build_result_text(results, consensus, task)

    def _handle_task_started(self, task_id: str) -> None:
        task = self.tasks.get(task_id)
        if task is None:
            return

        task.status = "运行中"
        task.result_text = "正在执行，请稍候..."
        task.touch()
        self._refresh_status_text()
        if not task.log_lines or task.log_lines[-1] != "[系统] 任务开始执行。":
            self._append_task_log(task_id, "[系统] 任务开始执行。")

    def _handle_task_results(self, payload: dict[str, object]) -> None:
        task_id = payload.get("task_id")
        results = payload.get("results")
        if not isinstance(task_id, str) or not isinstance(results, list):
            return
        typed_results = [item for item in results if isinstance(item, ProviderAnswer)]
        task = self.tasks.get(task_id)
        if task is None:
            return

        for item in typed_results:
            task.provider_results[item.provider_key] = item
            status = "失败" if item.error else "完成"
            pass

        task.recommendation, task.exact_ratio, task.result_text = self._build_live_result(task)

        task.status = "已完成" if typed_results and any(not item.error for item in typed_results) else "已失败"
        task.touch()

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
        self._mark_task_providers_stopped(task)
        task.touch()
        self._append_task_log(task_id, f"[系统] {task.result_text}")

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
        task.touch()
        self._append_task_log(task_id, f"[错误] {message}")

    def _handle_task_finished(self, payload: dict[str, object]) -> None:
        action = payload.get("action")
        task_id = payload.get("task_id")

        if action == "ask_all" and isinstance(task_id, str):
            self.running_task_ids.discard(task_id)
            self.task_stop_events.pop(task_id, None)
        elif action in {"warm_up", "close"}:
            self.control_action = None

        self._refresh_status_text()
        self._dispatch_pending_tasks_locked()

    def _mark_sessions_closed(self) -> None:
        for provider in PROVIDERS:
            if provider.key in self.last_selected_provider_keys:
                self.session_provider_states[provider.key] = ("已关闭", "-", "")
            else:
                self.session_provider_states[provider.key] = ("未选择", "-", "本次不会使用该平台")
        self._refresh_status_text()

    def _mark_task_providers_stopped(self, task: QueueTask, default_detail: str = "任务已被强制结束") -> None:
        for provider in task.providers:
            status, option, detail = task.provider_states.get(provider.key, ("待执行", "-", ""))
            if status in RUNNING_PROVIDER_STATUSES:
                task.provider_states[provider.key] = ("已终止", option, detail or default_detail)

    def _refresh_status_text(self) -> None:
        if self.control_action == "warm_up":
            return
        if self.control_action == "close":
            return

        running_count = len(self.running_task_ids)
        if running_count == 1:
            only_task_id = next(iter(self.running_task_ids))
            self.status_text = f"{only_task_id} 运行中..."
            return
        if running_count > 1:
            self.status_text = f"当前有 {running_count} 题并行处理中..."
            return

        pending_count = sum(1 for task in self.tasks.values() if task.status == "待执行")
        if pending_count:
            self.status_text = f"队列中还有 {pending_count} 题待执行"
            return
        self.status_text = "准备就绪"

    def _resolve_provider_keys(self, provider_keys: list[str]) -> tuple[ProviderConfig, ...]:
        selected_keys = set(provider_keys)
        selected = tuple(provider for provider in PROVIDERS if provider.key in selected_keys)
        if not selected:
            raise RuntimeError("请至少勾选一个平台。")
        return selected

    def _guard_control_action(self, action_name: str) -> None:
        if self.control_action is not None:
            raise RuntimeError("当前还有浏览器控制任务在运行，请稍等一下。")
        if any(task.status in ACTIVE_TASK_STATUSES for task in self.tasks.values()):
            raise RuntimeError(f"队列中还有任务，先等待完成或强制结束后再{action_name}。")

    def _guard_no_active_tasks(self, action_name: str) -> None:
        if any(task.status in ACTIVE_TASK_STATUSES for task in self.tasks.values()):
            raise RuntimeError(f"队列中还有任务，先等待完成或强制结束后再{action_name}。")

    def _split_questions(self, raw_text: str) -> list[str]:
        normalized = raw_text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not normalized:
            return []
        parts = [part.strip() for part in QUESTION_SPLIT_RE.split(normalized) if part.strip()]
        return parts or [normalized]

    def _question_title(self, question: str) -> str:
        for line in question.splitlines():
            stripped = line.strip()
            if stripped:
                return stripped[:80]
        return "未命名题目"

    def _create_task(
        self,
        question: str,
        providers: tuple[ProviderConfig, ...],
        timeout_seconds: int,
        browser_config: tuple[str, bool],
    ) -> QueueTask:
        self.task_counter += 1
        task_id = f"Q{self.task_counter:03d}"
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
            title=self._question_title(question),
            providers=providers,
            timeout_seconds=timeout_seconds,
            browser_config=browser_config,
            provider_states=provider_states,
            log_lines=["[系统] 任务已加入队列。"],
        )

    def _append_system_log(self, line: str) -> None:
        self.system_log_lines.append(line)
        _trim_lines(self.system_log_lines)

    def _append_task_log(self, task_id: str, line: str) -> None:
        task = self.tasks.get(task_id)
        if task is None:
            return
        task.log_lines.append(line)
        _trim_lines(task.log_lines)
        task.touch()

    def _set_session_provider_selection(self, selected_keys: set[str], selected_status: str) -> None:
        for provider in PROVIDERS:
            if provider.key in selected_keys:
                self.session_provider_states[provider.key] = (selected_status, "-", "")
            else:
                self.session_provider_states[provider.key] = ("未选择", "-", "本次不会使用该平台")

    def _build_result_text(
        self,
        results: list[ProviderAnswer],
        consensus,
        task: QueueTask | None = None,
    ) -> str:
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
        elif results:
            lines.append("当前已收到部分平台结果，但还没有稳定识别出推荐选项。")
        else:
            lines.append("正在等待各平台返回结果。")

        if task is not None:
            received_keys = {item.provider_key for item in results}
            pending_names = [provider.name for provider in task.providers if provider.key not in received_keys]
            if pending_names:
                lines.append(f"待返回平台：{' / '.join(pending_names)}")

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
