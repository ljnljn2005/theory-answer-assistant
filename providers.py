from __future__ import annotations

from dataclasses import dataclass


PROMPT_TEMPLATE = (
    "请只做选择题判定，并严格按以下纯文本格式回复，不要使用 markdown："
    "答案：<最可能的选项字母，单选只写一个字母，多选用英文逗号分隔，例如 A,C>；"
    "理由：<不超过50字>。如果拿不准，也必须给出当前最可能的选项。"
    "\n题目如下：\n{question}"
)


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    key: str
    name: str
    url: str
    input_selectors: tuple[str, ...] = ()
    submit_selectors: tuple[str, ...] = ()
    answer_selectors: tuple[str, ...] = ()
    popup_close_selectors: tuple[str, ...] = ()
    submit_key: str = "Enter"
    open_wait_ms: int = 1800


COMMON_INPUT_SELECTORS: tuple[str, ...] = (
    "textarea",
    "textarea.scroll-display-none",
    ".ql-editor[contenteditable='true']",
    "div.chat-input-editor[role='textbox']",
    "div[role='textbox']",
    "[contenteditable='true']",
    "input[type='text']",
)


COMMON_ANSWER_SELECTORS: tuple[str, ...] = (
    "[data-message-author-role='assistant']",
    "[data-testid*='assistant']",
    "[data-testid*='conversation-turn']",
    "[class*='assistant']",
    "[class*='message']",
    "article",
    "main section",
)


PROVIDERS: tuple[ProviderConfig, ...] = (
    ProviderConfig(
        key="deepseek",
        name="DeepSeek",
        url="https://chat.deepseek.com/",
        input_selectors=(
            "textarea",
            "div[role='textbox']",
            *COMMON_INPUT_SELECTORS,
        ),
        submit_selectors=(),
        answer_selectors=COMMON_ANSWER_SELECTORS,
        popup_close_selectors=("text=知道了", "text=关闭"),
    ),
    ProviderConfig(
        key="qianwen_cn",
        name="千问国内版",
        url="https://www.qianwen.com/",
        input_selectors=(
            "div[role='textbox']",
            "[contenteditable='true']",
            *COMMON_INPUT_SELECTORS,
        ),
        submit_selectors=("button[aria-label='发送消息']",),
        answer_selectors=COMMON_ANSWER_SELECTORS,
        popup_close_selectors=("text=关闭",),
    ),
    ProviderConfig(
        key="qwen",
        name="千问",
        url="https://chat.qwen.ai/",
        input_selectors=(
            "textarea",
            "[contenteditable='true']",
            "div[role='textbox']",
            *COMMON_INPUT_SELECTORS,
        ),
        submit_selectors=(),
        answer_selectors=(
            ".qwen-chat-message-assistant",
            "[data-message-author-role='assistant']",
            *COMMON_ANSWER_SELECTORS,
        ),
        popup_close_selectors=("text=稍后", "text=关闭"),
    ),
    ProviderConfig(
        key="zhipu",
        name="智谱清言",
        url="https://chatglm.cn/",
        input_selectors=(
            "textarea.scroll-display-none",
            "textarea",
            *COMMON_INPUT_SELECTORS,
        ),
        submit_selectors=(),
        answer_selectors=COMMON_ANSWER_SELECTORS,
        popup_close_selectors=("button.close-btn", "text=关闭", "text=知道了"),
        open_wait_ms=2200,
    ),
    ProviderConfig(
        key="kimi",
        name="Kimi",
        url="https://www.kimi.com/",
        input_selectors=(
            "div.chat-input-editor[role='textbox']",
            "div[role='textbox']",
            *COMMON_INPUT_SELECTORS,
        ),
        submit_selectors=(".send-button-container",),
        answer_selectors=(
            ".chat-content-item-assistant",
            *COMMON_ANSWER_SELECTORS,
        ),
        popup_close_selectors=("text=知道了", "text=关闭"),
    ),
    ProviderConfig(
        key="yuanbao",
        name="腾讯元宝",
        url="https://yuanbao.tencent.com/chat/naQivTmsDa",
        input_selectors=(
            ".ql-editor[contenteditable='true']",
            "[contenteditable='true']",
            *COMMON_INPUT_SELECTORS,
        ),
        submit_selectors=(),
        answer_selectors=COMMON_ANSWER_SELECTORS,
        popup_close_selectors=(".hyc-login__close", "text=关闭"),
        open_wait_ms=2200,
    ),
)


def build_prompt(question: str) -> str:
    return PROMPT_TEMPLATE.format(question=question.strip())
