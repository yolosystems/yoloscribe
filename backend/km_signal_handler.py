"""KMSignalHandler — emits typed KM signals in response to io-class mutation events.

The event-bus subscriber that replaces YOL-490's explicit ``_emit_km_signal``
calls in the MCP tool bodies: with the write-path pub/sub unification there is
one emission point (the ``yoloscribe_io`` file-class write) and the KM signal
becomes just another subscriber alongside on_write dispatch (``OnWriteEventHandler``)
and the notification bus (``NotificationBusHandler``). Attached to every
mutation-emitting file class by the backend file-object factory, so a mutation
from *any* caller — a user, the first-party runner, or a 3P runtime over MCP —
fans out an appropriately-typed KM signal to the configured SignalSink(s).

Only the mutation-shaped signals that map cleanly to an io-class event are
handled here: ``page_structured`` (page.created), ``content_routed``
(page.written), ``agent_provisioned`` (agent.created). Decision signals that
have no mutation to hang on — ``notification_sent``, ``proposal_accepted``/
``_rejected``, ``notification_suppressed``, ``user_instruction`` — remain
best-effort explicit emissions on their own code paths.

Best-effort and off the write path: ``signal_sinks.dispatch`` offloads to a
background thread and never raises, and ``EventEmitter._emit`` already swallows
handler exceptions.
"""

from __future__ import annotations

import km_signals
from signal_sinks import dispatch
from yoloscribe_io import Event, EventHandler, EventType


class KMSignalHandler(EventHandler):
    """Fan a typed KM signal out to configured SignalSink(s) for a mutation event.

    Stateless: ``site`` and the signal params are read from the (enriched)
    event payload, so one instance serves every site.
    """

    def handle(self, event: Event) -> None:
        payload = event.payload or {}
        site = str(payload.get("site", ""))
        if not site:
            return
        built = self._build_signal(event.type, payload)
        if built is None:
            return
        signal_type, params = built
        dispatch(site, signal_type, params)

    @staticmethod
    def _build_signal(event_type: str, payload: dict) -> tuple[str, dict] | None:
        page_path = str(payload.get("page_path", ""))
        reason = str(payload.get("reason", ""))
        if event_type == EventType.PAGE_CREATED:
            return km_signals.page_structured_signal(
                page_path, str(payload.get("content", "")), reason
            )
        if event_type == EventType.PAGE_WRITTEN:
            return km_signals.content_routed_signal(page_path, reason=reason)
        if event_type == EventType.AGENT_CREATED:
            return km_signals.agent_provisioned_signal(
                page_path,
                str(payload.get("agent_type", "")),
                list(payload.get("skills", []) or []),
                str(payload.get("trigger", "")),
            )
        return None
