"""YoloBrainSignalSink — delivers KM signals to YoloBrain's internal endpoint (YOL-558).

The other half of the transport whose receiving end is YoloBrain's
`POST /internal/signals` (YOL-557). Until this existed, signals were built
correctly — with `reason` populated (YOL-527) and accepted on arrival
(YOL-550) — and then delivered nowhere: `create_signal_sink` returned only the
generic webhook sink, and `signal_sinks/__init__` said as much in a comment.

## Why this routes by user rather than by site

`SignalSink.emit` is site-keyed, but YoloBrain's workspace is a *user*
(`engine.submit_signal(user_sub, ...)`). The obvious fix — look up the site's
owner — is wrong twice over. It needs a reverse index that does not exist
(`UserSiteRepository` has `get_site_for_user` and no inverse; the DynamoDB
table is keyed on user_id, so it would take a new GSI), and it would attribute
a shared-write user's edit to the site owner instead of to the person who made
it. The actor is already carried on the mutation event, so it is both cheaper
and more correct.

## Identity

`user_id` here is the `sub` from the shared IdP, which is byte-identical to
YoloBrain's `user_id` when both products point at the same discovery URL
(YOL-556). No mapping table, no crosswalk.

## Trust

The secret this sink holds can act as **any** user on the YoloBrain side, so it
must never be shared with a component that processes untrusted input, and
`base_url` must be the in-cluster service address — the endpoint is blocked at
the edge and pod → ClusterIP → pod never traverses the load balancer.
"""

from __future__ import annotations

import logging

import httpx

from .base import SignalSink

log = logging.getLogger(__name__)

# Short by design: this runs on a background thread alongside a wiki write, and
# a slow YoloBrain must not pile up executor threads. Losing a signal is the
# accepted failure mode; the local SignalLog still has it.
_TIMEOUT_SECONDS = 5.0


class YoloBrainSignalSink(SignalSink):
    """POSTs {user_sub, signal_type, params} to YoloBrain's internal signal endpoint."""

    def __init__(self, base_url: str, secret: str) -> None:
        self._url = f"{base_url.rstrip('/')}/internal/signals"
        self._secret = secret

    def emit(self, site: str, signal_type: str, payload: dict, user_id: str = "") -> None:
        if not user_id:
            # No subject means no workspace to route to. Skipping is the
            # honest outcome — sending an empty sub would file the signal under
            # a workspace keyed on the empty string, quietly polluting a real
            # user's memory with someone else's activity.
            #
            # No emitting path produces this today: MCP, REST, and the runner
            # all carry a resolved user. It guards the `user_id: str = ""`
            # default on S3Tools in agents/base.py, and any future
            # make_wiki_page caller that omits the actor. Debug rather than
            # warning because if it ever does fire it will fire constantly.
            log.debug(
                "YoloBrainSignalSink: skipping %s for site %s — no actor on the event",
                signal_type,
                site,
            )
            return

        body = {"user_sub": user_id, "signal_type": signal_type, "params": payload}
        try:
            with httpx.Client(timeout=_TIMEOUT_SECONDS) as client:
                resp = client.post(
                    self._url, json=body, headers={"X-Internal-Auth": self._secret}
                )
                resp.raise_for_status()
        except Exception as exc:
            # Best-effort by contract. A YoloBrain outage degrades to lost
            # signals, never to a failed wiki write. Logged at warning so the
            # loss is visible without being alarming.
            log.warning(
                "YoloBrainSignalSink delivery of %s for %s failed: %s", signal_type, site, exc
            )
