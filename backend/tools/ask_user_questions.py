"""计划模式中的用户澄清工具。"""

from approval_policy import AUTO_APPROVE
from tool_recovery import REPLAY_NEVER
from tool_registry import ToolSpec
from tool_scheduler import SEQUENTIAL
from tool_runtime import ToolOutput


def normalize_questions(questions):
    """校验并规范化模型提出的问题，保证前端能稳定渲染。"""
    if not isinstance(questions, list) or not 1 <= len(questions) <= 8:
        raise ValueError("questions 必须是 1 到 8 个问题的数组")

    normalized = []
    seen_ids = set()
    for item in questions:
        if not isinstance(item, dict):
            raise ValueError("每个问题必须是对象")
        question_id = str(item.get("id", "")).strip()
        question = str(item.get("question", "")).strip()
        if not question_id or not question:
            raise ValueError("每个问题都需要 id 和 question")
        if question_id in seen_ids:
            raise ValueError("问题 id 不能重复")
        if len(question_id) > 80 or len(question) > 500:
            raise ValueError("问题 id 最多 80 个字符，问题最多 500 个字符")
        options = item.get("options", [])
        if options is None:
            options = []
        if not isinstance(options, list) or len(options) > 12:
            raise ValueError("options 必须是最多 12 项的数组")
        normalized_options = [str(option).strip() for option in options]
        if any(not option or len(option) > 200 for option in normalized_options):
            raise ValueError("每个选项必须是 1 到 200 个字符")
        seen_ids.add(question_id)
        normalized.append(
            {
                "id": question_id,
                "question": question,
                "options": normalized_options,
            }
        )
    return normalized


def ask_user_questions(questions):
    """向宿主提交问题并等待用户回答；等待由 Web Harness 负责。"""
    normalize_questions(questions)
    return ToolOutput(
        transcript_content="Waiting for user answers.",
    )


ASK_USER_QUESTIONS_TOOL = {
    "type": "function",
    "function": {
        "name": "ask_user_questions",
        "description": (
            "Ask the user one clarifying question and wait for the answer. "
            "Use this in plan mode when important requirements are ambiguous; "
            "do not use it for information the agent can safely infer. "
            "In plan mode, make exactly one question per call and ask the next question "
            "only after the user answers this one."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "description": "Exactly one question per call. Include concrete options when useful; the host also provides an Other option.",
                    "minItems": 1,
                    "maxItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "question": {"type": "string"},
                            "options": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": ["id", "question"],
                    },
                },
            },
            "required": ["questions"],
        },
    },
}


TOOL_SPEC = ToolSpec(
    name="ask_user_questions",
    definition=ASK_USER_QUESTIONS_TOOL,
    implementation=ask_user_questions,
    execution_mode=SEQUENTIAL,
    approval_mode=AUTO_APPROVE,
    replay_policy=REPLAY_NEVER,
    plan_mode_visible=True,
)
