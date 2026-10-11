"""Exécution d'une tâche par un agent Claude Code, avec validation humaine."""

from __future__ import annotations

import asyncio
import json
import time

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from . import services as svc
from .policy import ALLOW, DENY, Policy
from .router import Project
from .store import Approval, Store, Task, new_id

SYSTEM_APPEND = """
Tu es piloté à distance depuis un cockpit mobile. L'utilisateur lit tes réponses sur téléphone :
- sois concis, termine par un résumé clair de ce qui a été fait et de ce qui reste à faire ;
- tu ne peux pas poser de question en cours de tâche : si une information manque, arrête-toi
  et formule la question dans ta réponse finale ;
- certaines actions nécessitent une validation humaine ; si une action est refusée, ne tente
  pas de la contourner par un autre moyen, explique plutôt ce qui était prévu.
"""


SERVICES_APPEND = """
Tu peux lancer, lister et arrêter des services Onyxia (chacun devient un projet pilotable) avec les outils
lancer_service, lister_services et arreter_service. N'utilise pas helm directement pour cela.
"""


def _short(value, limit: int = 600) -> str:
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return s if len(s) <= limit else s[:limit] + "…"


class Runner:
    def __init__(self, store: Store, policy: Policy, services: bool = False):
        self.services = services  # orchestrateur : outils de gestion des services
        self.store = store
        self.policy = policy
        self.locks: dict[str, asyncio.Lock] = {}
        self.clients: dict[str, ClaudeSDKClient] = {}  # task_id -> client actif

    def _lock(self, project: str) -> asyncio.Lock:
        return self.locks.setdefault(project, asyncio.Lock())

    async def cancel(self, task: Task) -> None:
        client = self.clients.get(task.id)
        if client:
            await client.interrupt()
        for a in self.store.approvals.values():
            if a.task_id == task.id and a.future and not a.future.done():
                a.future.set_result("deny")
        self.store.task_update(task, status="cancelled")

    async def request_approval(self, task: Task, project: Project, tool: str, tool_input: dict, reason: str) -> bool:
        loop = asyncio.get_running_loop()
        approval = Approval(
            id=new_id(), task_id=task.id, project=project.id, tool=tool,
            tool_input=tool_input, reason=reason,
            expires=time.time() + self.policy.timeout, future=loop.create_future(),
        )
        self.store.approvals[approval.id] = approval
        self.store.task_update(task, status="waiting_approval")
        self.store.publish("approval", {"task_id": task.id, "approval": approval.public()})
        try:
            decision = await asyncio.wait_for(approval.future, timeout=self.policy.timeout)
        except asyncio.TimeoutError:
            decision = "timeout"
        approval.decision = decision
        self.store.publish("approval", {"task_id": task.id, "approval": approval.public()})
        self.store.audit("approval", task=task.id, project=project.id, tool=tool,
                         input=tool_input, reason=reason, decision=decision)
        if task.status == "waiting_approval":
            self.store.task_update(task, status="running")
        return decision == "approve"

    def _options(self, task: Task, project: Project, resume: bool) -> ClaudeAgentOptions:
        approved: set[str] = set()

        async def pre_tool(input_data, tool_use_id, context):
            tool = input_data["tool_name"]
            tool_input = input_data.get("tool_input", {})
            d = self.policy.evaluate(tool, tool_input, project.cwd)
            if d.action != DENY and tool.startswith(svc.TOOL_PREFIX):
                d.reason = await svc.describe(tool, tool_input) or d.reason
            if d.action == ALLOW:
                ok = True
            elif d.action == DENY:
                ok = False
            else:
                ok = await self.request_approval(task, project, tool, tool_input, d.reason)
            self.store.audit("tool", task=task.id, project=project.id, tool=tool,
                             input=_short(tool_input, 2000), policy=d.action, reason=d.reason, allowed=ok)
            if ok:
                approved.add(input_data.get("tool_use_id") or tool_use_id or "")
                decision, why = "allow", d.reason
            else:
                why = d.reason if d.action == DENY else f"refusé par l'utilisateur : {d.reason}"
                self.store.publish("message", {"task_id": task.id, "type": "blocked", "tool": tool, "text": why})
                decision = "deny"
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision,
                "permissionDecisionReason": why,
            }}

        async def can_use_tool(tool, tool_input, context):
            # Filet de sécurité : n'autorise que ce que le hook a déjà approuvé.
            if context.tool_use_id in approved:
                return PermissionResultAllow()
            return PermissionResultDeny(message="non approuvé par la politique du cockpit")

        return ClaudeAgentOptions(
            cwd=project.cwd,
            system_prompt={"type": "preset", "preset": "claude_code",
                           "append": SYSTEM_APPEND + (SERVICES_APPEND if self.services else "")},
            mcp_servers={svc.SERVER_NAME: svc.server()} if self.services else {},
            permission_mode="default",
            setting_sources=["project"],  # CLAUDE.md du projet, sans les permissions utilisateur
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[pre_tool],
                                              timeout=self.policy.timeout + 60)]},
            can_use_tool=can_use_tool,
            resume=self.store.sessions.get(project.id) if resume else None,
        )

    async def run(self, task: Task, project: Project, resume: bool = True) -> None:
        self.store.task_update(task, status="queued", project=project.id)
        async with self._lock(project.id):
            if task.status == "cancelled":
                return
            self.store.task_update(task, status="running")
            self.store.audit("task_start", task=task.id, project=project.id, instruction=task.instruction)
            try:
                async with ClaudeSDKClient(self._options(task, project, resume)) as client:
                    self.clients[task.id] = client
                    await client.query(task.instruction)
                    async for msg in client.receive_response():
                        self._relay(task, msg)
            except Exception as e:
                self.store.task_update(task, status="error", result=f"{type(e).__name__}: {e}")
            finally:
                self.clients.pop(task.id, None)
            self.store.audit("task_end", task=task.id, project=project.id, status=task.status,
                             cost_usd=task.cost_usd, result=_short(task.result, 2000))

    def _relay(self, task: Task, msg) -> None:
        pub = lambda **kw: self.store.publish("message", {"task_id": task.id, **kw})
        if isinstance(msg, AssistantMessage):
            for b in msg.content:
                if isinstance(b, TextBlock) and b.text.strip():
                    pub(type="text", text=b.text)
                elif isinstance(b, ToolUseBlock):
                    pub(type="tool_use", tool=b.name, text=_short(b.input, 300))
        elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
            for b in msg.content:
                if isinstance(b, ToolResultBlock) and b.is_error:
                    pub(type="tool_error", text=_short(b.content, 300))
        elif isinstance(msg, ResultMessage):
            self.store.save_session(task.project, msg.session_id)
            if task.status != "cancelled":
                self.store.task_update(
                    task,
                    status="error" if msg.is_error else "done",
                    result=msg.result or "",
                    session_id=msg.session_id,
                    cost_usd=msg.total_cost_usd or 0.0,
                )
