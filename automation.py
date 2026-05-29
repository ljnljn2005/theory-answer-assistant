from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import re
import time
from pathlib import Path
from typing import Callable

from consensus import ProviderAnswer, extract_options
from providers import ProviderConfig, build_prompt

try:
    from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright
except ImportError:  # pragma: no cover - import guard for first-run setup
    Locator = object  # type: ignore[assignment]
    Page = object  # type: ignore[assignment]
    PlaywrightTimeoutError = TimeoutError  # type: ignore[assignment]
    sync_playwright = None


StatusCallback = Callable[[str, str, str], None]

ANTI_DETECTION_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en-US', 'en'] });
Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
window.chrome = window.chrome || { runtime: {} };
"""

EXTRACT_ANSWER_SCRIPT = """
(selectors) => {
  const isVisible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    if (!style) return false;
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };

  const isEditable = (el) => {
    if (!el) return false;
    if (el.isContentEditable) return true;
    const tag = (el.tagName || '').toLowerCase();
    return tag === 'textarea' || tag === 'input';
  };

  const regex = /(答案|answer)\\s*[:：]\\s*[A-Za-z](?:\\s*[,，、/|]\\s*[A-Za-z])*/i;
  const pool = [];
  const seen = new Set();

  const pushText = (el) => {
    if (!isVisible(el) || isEditable(el)) return;
    const text = (el.innerText || el.textContent || '').trim();
    if (!text || text.length < 4 || seen.has(text)) return;
    seen.add(text);
    const rect = el.getBoundingClientRect();
    const answerBoost = regex.test(text) ? 100000 : 0;
    const positionBoost = Math.max(rect.top, -2000);
    const sizeBoost = Math.min(text.length, 600);
    pool.push({
      text,
      score: answerBoost + positionBoost + sizeBoost,
    });
  };

  for (const selector of selectors) {
    for (const el of document.querySelectorAll(selector)) {
      pushText(el);
    }
  }

  if (!pool.length) {
    for (const el of document.querySelectorAll('main *, article, section, div, p, li, span')) {
      pushText(el);
    }
  }

  pool.sort((a, b) => a.score - b.score);
  return pool.length ? pool[pool.length - 1].text : '';
}
"""

READ_EDITOR_TEXT_SCRIPT = """
(el) => {
  if (!el) return '';
  if (el.isContentEditable) {
    return (el.innerText || el.textContent || '').replace(/\\u00a0/g, ' ');
  }
  return (el.value || '').replace(/\\u00a0/g, ' ');
}
"""

ANSWER_MARKER_RE = re.compile(r"(?:答案|answer)\s*[:：]", re.IGNORECASE)

class ProviderRunner:
    def __init__(
        self,
        provider: ProviderConfig,
        profile_root: Path,
        playwright,
        channel: str,
        headless: bool,
        status_callback: StatusCallback | None = None,
    ) -> None:
        self.provider = provider
        self.profile_dir = profile_root / provider.key
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.playwright = playwright
        self.channel = channel
        self.headless = headless
        self.status_callback = status_callback
        self.context = None
        self.page = None
        self._observed_page = None
        self._last_submission_error = ""

    def open(self) -> None:
        page = self._ensure_page()
        self._last_submission_error = ""
        self._notify("opening", "已打开网页")
        page.goto(self.provider.url, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(self.provider.open_wait_ms)
        self._dismiss_interfering_ui(page)
        if self._find_input(page) is None:
            self._notify("waiting_login", "请在浏览器中完成登录后再提问")
        else:
            self._notify("ready", "已就绪")

    def ask(self, question: str, timeout_seconds: int) -> ProviderAnswer:
        page = self._ensure_page()
        self._last_submission_error = ""
        self._notify("navigating", "正在进入聊天页")
        page.goto(self.provider.url, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(self.provider.open_wait_ms)
        self._dismiss_interfering_ui(page)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")

        prompt = build_prompt(question)
        editor = self._find_input(page)
        if editor is None:
            raise RuntimeError("没有找到输入框，请先登录并确认已经进入聊天界面。")

        self._notify("typing", "正在直接写入题目")
        self._fill_prompt(page, editor, prompt)
        before_answer = self._extract_answer(page)

        self._notify("submitting", "正在按 Enter 提交")
        self._submit(page, editor)

        self._notify("waiting_answer", "正在等待回答")
        answer_text = self._wait_for_answer(page, timeout_seconds, before_answer)
        parsed_options = extract_options(answer_text)

        self._notify(
            "done",
            f"已提取答案：{','.join(parsed_options) if parsed_options else '未识别选项'}",
        )
        return ProviderAnswer(
            provider_key=self.provider.key,
            provider_name=self.provider.name,
            raw_answer=answer_text,
            parsed_options=parsed_options,
        )

    def close(self) -> None:
        if self.context is not None:
            try:
                self.context.close()
            finally:
                self.context = None
                self.page = None

    def _ensure_page(self):
        if self.context is None:
            self._notify("launching", "正在启动浏览器")
            self.context = self.playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                channel=self.channel,
                headless=self.headless,
                viewport={"width": 1440, "height": 960} if self.headless else None,
                ignore_default_args=["--enable-automation"],
                args=["--disable-blink-features=AutomationControlled"],
            )
            self.context.add_init_script(ANTI_DETECTION_SCRIPT)
            self.context.set_default_timeout(20000)

        if self.page is None or self.page.is_closed():
            pages = [page for page in self.context.pages if not page.is_closed()]
            self.page = pages[0] if pages else self.context.new_page()

        if self._observed_page is not self.page:
            self.page.on("response", self._handle_response)
            self._observed_page = self.page

        return self.page

    def _dismiss_interfering_ui(self, page: Page) -> None:
        for _ in range(2):
            closed_any = False
            for selector in self.provider.popup_close_selectors:
                try:
                    locator = page.locator(selector).first
                    if locator.count() and locator.is_visible():
                        locator.click(timeout=2500)
                        page.wait_for_timeout(500)
                        closed_any = True
                except Exception:
                    continue
            if not closed_any:
                return

    def _find_input(self, page: Page) -> Locator | None:
        for selector in self.provider.input_selectors:
            locator = page.locator(selector)
            try:
                count = min(locator.count(), 6)
            except Exception:
                continue
            for index in range(count - 1, -1, -1):
                candidate = locator.nth(index)
                try:
                    if candidate.is_visible() and candidate.is_enabled():
                        return candidate
                except Exception:
                    continue
        return None

    def _fill_prompt(self, page: Page, editor: Locator, text: str) -> None:
        self._focus_editor(page, editor)
        page.keyboard.press("Control+A")
        page.wait_for_timeout(80)
        page.keyboard.press("Backspace")
        page.wait_for_timeout(120)
        input_mode = editor.evaluate(
            "(el) => el && (el.isContentEditable ? 'contenteditable' : (el.tagName || '').toLowerCase())"
        )

        if input_mode in {"textarea", "input"}:
            editor.fill(text)
        elif self.provider.key == "yiyan":
            page.keyboard.type(text, delay=35)
        else:
            page.keyboard.insert_text(text)

        current = self._normalize_text(self._read_editor_text(editor))
        expected = self._normalize_text(text)
        if self.provider.key == "yiyan":
            if not current:
                raise RuntimeError("输入框内容写入不完整，请确认当前平台聊天页处于可输入状态。")
            return
        if current != expected:
            raise RuntimeError("输入框内容写入不完整，请确认当前平台聊天页处于可输入状态。")

    def _focus_editor(self, page: Page, editor: Locator) -> None:
        editor.click(timeout=6000, force=True)
        page.wait_for_timeout(150)

    def _submit(self, page: Page, editor: Locator) -> None:
        self._dismiss_interfering_ui(page)
        self._focus_editor(page, editor)
        if self.provider.key == "yiyan":
            self._submit_with_response_capture(page, editor)
            return

        page.keyboard.press(self.provider.submit_key)
        page.wait_for_timeout(800)

        current_after_enter = self._normalize_text(self._read_editor_text(editor))
        if current_after_enter and self.provider.submit_selectors:
            target = self._find_submit_target(page)
            if target is not None:
                try:
                    target.click(timeout=4000, force=True)
                    page.wait_for_timeout(1200)
                except Exception:
                    pass

    def _submit_with_response_capture(self, page: Page, editor: Locator) -> None:
        response = None
        try:
            with page.expect_response(lambda item: "/eb/chat/conversation/v2" in item.url, timeout=10000) as pending:
                page.keyboard.press(self.provider.submit_key)
                page.wait_for_timeout(800)

                current_after_enter = self._normalize_text(self._read_editor_text(editor))
                if current_after_enter and self.provider.submit_selectors:
                    target = self._find_submit_target(page)
                    if target is not None:
                        target.click(timeout=4000, force=True)
                        page.wait_for_timeout(1200)
            response = pending.value
        except Exception:
            return

        self._remember_submission_error(response)

    def _wait_for_answer(self, page: Page, timeout_seconds: int, previous_answer: str) -> str:
        deadline = time.monotonic() + timeout_seconds
        stable_hits = 0
        last_text = ""
        best_text = ""
        previous_normalized = self._normalize_text(previous_answer)

        while time.monotonic() < deadline:
            if self._last_submission_error:
                raise RuntimeError(f"{self.provider.name} 返回错误：{self._last_submission_error}")
            page.wait_for_timeout(2000)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            current = self._extract_answer(page)
            if not self._looks_like_answer(current):
                body_fallback = self._extract_answer_from_body(page)
                if self._looks_like_answer(body_fallback):
                    current = body_fallback
            current_normalized = self._normalize_text(current)
            if not current_normalized or current_normalized == previous_normalized:
                continue

            best_text = current
            if current == last_text:
                stable_hits += 1
            else:
                last_text = current
                stable_hits = 1

            if stable_hits >= 2 and self._looks_like_answer(current):
                return current

        if best_text:
            return best_text
        raise PlaywrightTimeoutError(f"{self.provider.name} 在 {timeout_seconds} 秒内没有返回可提取答案。")

    def _extract_answer(self, page: Page) -> str:
        try:
            text = page.evaluate(EXTRACT_ANSWER_SCRIPT, list(self.provider.answer_selectors))
            return (text or "").strip()
        except Exception:
            return ""

    def _extract_answer_from_body(self, page: Page) -> str:
        try:
            body_text = page.locator("body").inner_text(timeout=6000)
        except Exception:
            return ""
        return self._extract_latest_answer_block(body_text)

    def _read_editor_text(self, editor: Locator) -> str:
        try:
            return editor.evaluate(READ_EDITOR_TEXT_SCRIPT) or ""
        except Exception:
            return ""

    def _looks_like_answer(self, text: str) -> bool:
        if extract_options(text):
            return True
        marker = re.search("(?:\u7b54\u6848|answer)\\s*[:\uFF1A]", text, re.IGNORECASE)
        if not marker:
            return False
        snippet = text[marker.start() : marker.start() + 120]
        return "<" not in snippet[:80]

    def _find_submit_target(self, page: Page) -> Locator | None:
        for selector in self.provider.submit_selectors:
            locator = page.locator(selector)
            try:
                count = min(locator.count(), 4)
            except Exception:
                continue
            for index in range(count):
                candidate = locator.nth(index)
                try:
                    if candidate.is_visible():
                        return candidate
                except Exception:
                    continue
        return None

    def _normalize_text(self, text: str) -> str:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
        normalized = "\n".join(line.rstrip() for line in normalized.split("\n"))
        normalized = re.sub(r"\n{2,}", "\n", normalized)
        return normalized.strip()

    def _extract_latest_answer_block(self, body_text: str) -> str:
        matches = list(ANSWER_MARKER_RE.finditer(body_text or ""))
        while matches:
            match = matches.pop()
            snippet = body_text[match.start() : match.start() + 500]
            if "<" in snippet[:80]:
                continue

            lines = [line.strip() for line in snippet.splitlines()]
            collected: list[str] = []
            for line in lines:
                if not line:
                    if collected:
                        break
                    continue
                collected.append(line)
                if len(collected) >= 3:
                    break

            candidate = "\n".join(collected).strip()
            if extract_options(candidate):
                return candidate
        return ""

    def _notify(self, status: str, message: str) -> None:
        if self.status_callback is not None:
            self.status_callback(self.provider.key, status, message)

    def _handle_response(self, response) -> None:
        if self.provider.key != "yiyan":
            return
        if "/eb/chat/conversation/v2" not in response.url:
            return
        self._remember_submission_error(response)

    def _remember_submission_error(self, response) -> None:
        try:
            payload = self._parse_response_payload(response.text())
        except Exception:
            return
        if not isinstance(payload, dict):
            return
        code = payload.get("code")
        if code not in (None, 0):
            self._last_submission_error = str(payload.get("msg") or "提交失败")

    def _parse_response_payload(self, raw_text: str) -> dict | None:
        text = (raw_text or "").strip()
        if not text:
            return None
        if text.startswith("event:"):
            data_lines = [line[5:] for line in text.splitlines() if line.startswith("data:")]
            if not data_lines:
                return None
            text = "\n".join(data_lines).strip()
        return json.loads(text)


class AutomationManager:
    def __init__(
        self,
        workspace_root: Path,
        channel: str = "msedge",
        headless: bool = False,
        status_callback: StatusCallback | None = None,
    ) -> None:
        if sync_playwright is None:
            raise RuntimeError(
                "未安装 Playwright。请先执行 `pip install -r requirements.txt`，然后执行 "
                "`python -m playwright install chromium`。"
            )

        self.workspace_root = workspace_root
        self.profile_root = workspace_root / "profiles"
        self.profile_root.mkdir(parents=True, exist_ok=True)
        self.channel = channel
        self.headless = headless
        self.status_callback = status_callback
        self._playwright_manager = sync_playwright().start()
        self.runners: dict[str, ProviderRunner] = {}

    def warm_up(self, providers: tuple[ProviderConfig, ...]) -> None:
        for provider in providers:
            runner = self._get_runner(provider)
            runner.open()

    def ask_all(
        self,
        providers: tuple[ProviderConfig, ...],
        question: str,
        timeout_seconds: int,
    ) -> list[ProviderAnswer]:
        if self.headless and len(providers) > 1:
            return self._ask_all_parallel(providers, question, timeout_seconds)

        results: list[ProviderAnswer] = []
        for provider in providers:
            runner = self._get_runner(provider)
            try:
                result = runner.ask(question, timeout_seconds)
            except Exception as exc:
                runner._notify("error", str(exc))
                result = ProviderAnswer(
                    provider_key=provider.key,
                    provider_name=provider.name,
                    error=str(exc),
                )
            results.append(result)
        return results

    def _ask_all_parallel(
        self,
        providers: tuple[ProviderConfig, ...],
        question: str,
        timeout_seconds: int,
    ) -> list[ProviderAnswer]:
        results_by_key: dict[str, ProviderAnswer] = {}

        with ThreadPoolExecutor(max_workers=len(providers), thread_name_prefix="headless-ask") as executor:
            future_map = {
                executor.submit(self._ask_provider_isolated, provider, question, timeout_seconds): provider
                for provider in providers
            }
            for future in as_completed(future_map):
                provider = future_map[future]
                try:
                    result = future.result()
                except Exception as exc:
                    if self.status_callback is not None:
                        self.status_callback(provider.key, "error", str(exc))
                    result = ProviderAnswer(
                        provider_key=provider.key,
                        provider_name=provider.name,
                        error=str(exc),
                    )
                results_by_key[provider.key] = result

        return [results_by_key[provider.key] for provider in providers]

    def _ask_provider_isolated(
        self,
        provider: ProviderConfig,
        question: str,
        timeout_seconds: int,
    ) -> ProviderAnswer:
        if sync_playwright is None:
            raise RuntimeError("Playwright 不可用。")

        playwright_manager = sync_playwright().start()
        runner: ProviderRunner | None = None
        try:
            runner = ProviderRunner(
                provider=provider,
                profile_root=self.profile_root,
                playwright=playwright_manager,
                channel=self.channel,
                headless=self.headless,
                status_callback=self.status_callback,
            )
            return runner.ask(question, timeout_seconds)
        except Exception as exc:
            if runner is not None:
                runner._notify("error", str(exc))
            elif self.status_callback is not None:
                self.status_callback(provider.key, "error", str(exc))
            return ProviderAnswer(
                provider_key=provider.key,
                provider_name=provider.name,
                error=str(exc),
            )
        finally:
            if runner is not None:
                runner.close()
            playwright_manager.stop()

    def close(self) -> None:
        for runner in self.runners.values():
            runner.close()
        self.runners.clear()
        if self._playwright_manager is not None:
            self._playwright_manager.stop()
            self._playwright_manager = None

    def _get_runner(self, provider: ProviderConfig) -> ProviderRunner:
        if provider.key not in self.runners:
            self.runners[provider.key] = ProviderRunner(
                provider=provider,
                profile_root=self.profile_root,
                playwright=self._playwright_manager,
                channel=self.channel,
                headless=self.headless,
                status_callback=self.status_callback,
            )
        return self.runners[provider.key]
