import os

from pathlib import Path
from skills import load_skill
from tools.registry import tool_description_lines


USER_PROMPT_PATH = Path("AGENTS.md")
PROJECT_INSTRUCTIONS_TAG = "project_instructions"


def xml_section(tag: str, content: str) -> str:
    return f"<{tag}>\n{content.strip()}\n</{tag}>"


def project_instructions_content() -> str:
    if not USER_PROMPT_PATH.exists():
        return ""
    return USER_PROMPT_PATH.read_text(encoding="utf-8").strip()


def project_instructions_user_content() -> str:
    content = project_instructions_content()
    if not content:
        return ""
    return xml_section(PROJECT_INSTRUCTIONS_TAG, content)


def is_project_instructions_message(message: dict) -> bool:
    content = message.get("content")
    return (
        message.get("role") == "user"
        and isinstance(content, str)
        and content.strip().startswith(f"<{PROJECT_INSTRUCTIONS_TAG}>")
    )


def ensure_project_instructions_message(messages: list[dict]) -> None:
    content = project_instructions_user_content()
    if not content:
        messages[:] = [
            message
            for message in messages
            if not is_project_instructions_message(message)
        ]
        return

    project_message = {"role": "user", "content": content}
    if messages and is_project_instructions_message(messages[0]):
        messages[0] = project_message
        return

    messages[:] = [
        message
        for message in messages
        if not is_project_instructions_message(message)
    ]
    messages.insert(0, project_message)


def build_main_system() -> str:
    return MAIN_SYSTEM


def build_subagent_system() -> str:
    return SUBAGENT_SYSTEM


def build_subagent_final_system() -> str:
    return SUBAGENT_SYSTEM + (
        "\n\n"
        "The tool budget is exhausted. Use the available tool results and return the final concise summary now."
    )


TASK_WORKFLOW_SECTION = (
    "Task workflow:\n"
    "- For non-trivial user requests, start or update the tracked task before making changes.\n"
    "- Use task_start only when beginning a new user-level task, with a short subject and a 2-5 item plan.\n"
    "- Treat task_start(plan=[...]) as the primary way to create the initial todo list.\n"
    "- Do not use task_start for internal workflow steps, retries, status checks, or tool failures.\n"
    "- Use todo_done as steps are completed, and keep todos focused on executable steps.\n"
    "- Use task_complete when the requested work is done.\n"
    "- Do not create tasks or todos for simple questions, tiny lookups, or one-step responses."
)


IDENTITY_SECTION = (
    f"You are Penhin Code, a tiny coding agent running in {os.getcwd()}."
)


TOOL_USAGE_SECTION = (
    "Use the available tools according to their descriptions. "
    "Prefer structured tools over ad hoc shell commands for file operations. "
    "Tool results are JSON with ok/message/data/error/meta fields; prefer data for structured facts and error for failures. "
)


FILE_SCOPE_SECTION = (
    "Ignore .venv, .git, __pycache__, skills, and internal state files."
)


def available_tools_section() -> str:
    return "Available tools:\n" + "\n".join(tool_description_lines())


def available_skills_section() -> str:
    return "Available skills:\n" + load_skill.get_descriptions()


def build_main_system_sections() -> list[str]:
    return [
        xml_section("identity", IDENTITY_SECTION),
        xml_section("tool_usage", TOOL_USAGE_SECTION),
        xml_section("file_scope", FILE_SCOPE_SECTION),
        xml_section("task_workflow", TASK_WORKFLOW_SECTION),
        xml_section("available_tools", available_tools_section()),
        xml_section("available_skills", available_skills_section()),
    ]


def build_main_system_base() -> str:
    return "\n\n".join(section.strip() for section in build_main_system_sections() if section.strip())


TASK_WORKFLOW = "\n\n" + TASK_WORKFLOW_SECTION
MAIN_SYSTEM = build_main_system_base()


SUBAGENT_SYSTEM = (
    "You are a focused subagent. "
    "Complete the assigned task independently and return a concise summary. "
    "Follow project instructions, but keep the assigned task narrow and do not expand scope. "
    "Tool results are JSON with ok/message/data/error/meta fields; prefer data for structured facts and error for failures. "
)


AUTO_COMPACT_SYSTEM = (
    "You summarize coding-agent conversation history into a compact "
    "state snapshot for continuing engineering work."
)
