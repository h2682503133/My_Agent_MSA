"""技能相关工具的实现声明（从 skill_runtime.dispatch 的 if 链搬出来）。

分工：
- 名字/别名/暴露 = `TOOL.json`；
- 能不能用 = 这里声明的 `requires`（clawhub / openviking 开关）；
- 差异化（本机 CLI vs 外部 VM）= `skill_runtime._run_clawhub` 里的 provider 选择（上层 `TOOL_PROVIDERS`）。
"""

from __future__ import annotations

from app import tool_registry
from app.skill_runtime import skill_runtime
from app.tool_registry import ToolContext

CLAWHUB_TOOLS = ("clawhub-search", "clawhub-install", "clawhub-list")
OPENVIKING_TOOLS = (
    "skill-list", "skill-list-simple", "skill-delete",
    "skill-abstract", "skill-overview", "skill-manual",
    "add-skill-to-viking",
)


def _skill(ctx: ToolContext):
    return ctx.skill or skill_runtime


def _clawhub_search(ctx: ToolContext) -> str:
    keyword = ctx.first("keyword", "query", "name", index=0)
    return _skill(ctx).clawhub_search(keyword, timeout=ctx.timeout)


def _clawhub_install(ctx: ToolContext) -> str:
    slug = ctx.first("skill_slug", "skill", "name", index=0)
    return _skill(ctx).clawhub_install(slug, timeout=ctx.timeout)


def _clawhub_list(ctx: ToolContext) -> str:
    return _skill(ctx).clawhub_list(timeout=ctx.timeout)


def _skill_list(ctx: ToolContext) -> str:
    return _skill(ctx).skill_list()


def _skill_list_simple(ctx: ToolContext) -> str:
    return _skill(ctx).skill_list_simple()


def _skill_delete(ctx: ToolContext) -> str:
    slug = ctx.first("skill_slug", "skill", "name", index=0)
    return _skill(ctx).skill_delete(slug, timeout=ctx.timeout)


def _skill_abstract(ctx: ToolContext) -> str:
    return _skill(ctx).skill_abstract(ctx.first("skill_name", "skill", "name", index=0))


def _skill_overview(ctx: ToolContext) -> str:
    return _skill(ctx).skill_overview(ctx.first("skill_name", "skill", "name", index=0))


def _skill_manual(ctx: ToolContext) -> str:
    return _skill(ctx).skill_manual(ctx.first("skill_name", "skill", "name", index=0))


def _add_skill_to_viking(ctx: ToolContext) -> str:
    return _skill(ctx).add_skill_to_viking(ctx.first("skill_slug", "skill", "name", index=0))


def _viking_search(ctx: ToolContext) -> str:
    query = ctx.first("query", "text", "关键词", index=0)
    scope = ctx.first("scope", "范围", index=1, default="session")
    top_k = ctx.first("top_k", "limit", "条数", index=2, default="8")
    return _skill(ctx).viking_search(
        query,
        scope=scope,
        top_k=top_k,
        user_id=str(ctx.kwargs.get("user_id") or ""),
        session_id=str(ctx.kwargs.get("session_id") or ""),
        agent_id=str(ctx.kwargs.get("agent_id") or ""),
    )


def _dsh(ctx: ToolContext) -> str:
    return _skill(ctx).dsh_tool(
        ctx.args,
        ctx.kwargs,
        ctx.root,
        ctx.timeout,
    )


tool_registry.register(
    "clawhub-search", _clawhub_search,
    aliases=("skill-search", "search-skill", "search_skill"), requires=("clawhub",),
    reason=(
        "错误：ClawHub 未启用。部署 tool-runtime 时未勾选「clawhub 技能执行」功能，"
        "无法搜索/安装/卸载技能。如需启用，请重新运行 deploy-all.ps1 并勾选 clawhub，"
        "或设置环境变量 ENABLE_CLAWHUB=true 后重启 tool-runtime。"
    ),
)
tool_registry.register(
    "clawhub-install", _clawhub_install,
    aliases=("skill-install", "install-skill", "install_skill", "download_skill"), requires=("clawhub",),
    reason=(
        "错误：ClawHub 未启用。部署 tool-runtime 时未勾选「clawhub 技能执行」功能，"
        "无法搜索/安装/卸载技能。如需启用，请重新运行 deploy-all.ps1 并勾选 clawhub，"
        "或设置环境变量 ENABLE_CLAWHUB=true 后重启 tool-runtime。"
    ),
)
tool_registry.register("clawhub-list", _clawhub_list, requires=("clawhub",))

_OPENVIKING_REASON = (
    "错误：OpenViking 未启用。部署时未勾选 openviking-server（技能知识库），"
    "无法查询或导入技能。如需启用，请重新运行 deploy-all.ps1 并勾选 openviking-server，"
    "或设置环境变量 ENABLE_OPENVIKING=true 后重启 tool-runtime。"
)

tool_registry.register("skill-list", _skill_list, requires=("openviking",), reason=_OPENVIKING_REASON)
tool_registry.register("skill-list-simple", _skill_list_simple, requires=("openviking",), reason=_OPENVIKING_REASON)
tool_registry.register("skill-delete", _skill_delete, requires=("openviking",), reason=_OPENVIKING_REASON)
tool_registry.register("skill-abstract", _skill_abstract, requires=("openviking",), reason=_OPENVIKING_REASON)
tool_registry.register("skill-overview", _skill_overview, requires=("openviking",), reason=_OPENVIKING_REASON)
tool_registry.register("skill-manual", _skill_manual, requires=("openviking",), reason=_OPENVIKING_REASON)
tool_registry.register("add-skill-to-viking", _add_skill_to_viking, requires=("openviking",), reason=_OPENVIKING_REASON)

tool_registry.register("viking-search", _viking_search, aliases=("viking-search-memory",))
tool_registry.register("dsh", _dsh, aliases=("dsh-chat",))
