"""Central factory for ``yoloscribe_io`` file objects with the standard reactive
handler set pre-attached (YOL-501).

This is the single seam that makes "a mutation is always reactive" *structural*
rather than something a new MCP tool can forget. Every mutation-emitting file
class is built here with its subscribers wired:

- ``OnWriteEventHandler`` — dispatches ``on_write`` agents on page-content writes
  (pages only; its own concern, unchanged).
- ``NotificationBusHandler`` — routes structural mutation events onto the
  notification bus → ``on_notify`` agents.
- ``KMSignalHandler`` — fans typed KM signals out to configured SignalSink(s).

Because the handlers are attached server-side (not by the caller), a mutation
made by a first-party user, the agent-runner with a run token, or a 3P runtime
over MCP all produce identical reactive side-effects.
"""

from __future__ import annotations

from km_signal_handler import KMSignalHandler
from queue_helpers import enqueue_agent_job, enqueue_notify_agent
from s3_storage import storage as _storage
from yoloscribe_io import (
    AgentMarkdownFile,
    NotificationBusHandler,
    OnWriteEventHandler,
    PageSettings,
    SkillMarkdownFile,
    WikiPageMarkdownFile,
)

# KMSignalHandler is stateless (site + params come from the event payload), so a
# single shared instance serves every site.
_KM_HANDLER = KMSignalHandler()


def _bus_handler(site: str) -> NotificationBusHandler:
    return NotificationBusHandler(site, _storage, enqueue=enqueue_notify_agent)


def make_wiki_page(site: str, page_path: str) -> WikiPageMarkdownFile:
    """A wiki page with on_write dispatch + notification bus + KM signals wired."""
    wiki = WikiPageMarkdownFile(site=site, page_path=page_path, storage=_storage)
    wiki.add_handler(OnWriteEventHandler(storage=_storage, enqueue=enqueue_agent_job))
    wiki.add_handler(_bus_handler(site))
    wiki.add_handler(_KM_HANDLER)
    return wiki


def make_agent_file(site: str, page_path: str, agent_name: str) -> AgentMarkdownFile:
    """An agent definition file with notification bus + KM signals wired.

    No ``OnWriteEventHandler`` — agents aren't wiki pages; agent.created carries
    the ``agent_provisioned`` KM signal via ``KMSignalHandler``.
    """
    agent = AgentMarkdownFile(site=site, page_path=page_path, agent_name=agent_name, storage=_storage)
    agent.add_handler(_bus_handler(site))
    agent.add_handler(_KM_HANDLER)
    return agent


def make_skill_file(site: str, skill_name: str) -> SkillMarkdownFile:
    """A skill definition file with the notification bus wired.

    No KM signal type maps to a skill mutation today, so only the bus handler is
    attached (skill.created/changed/deleted → on_notify).
    """
    skill = SkillMarkdownFile(site=site, skill_name=skill_name, storage=_storage)
    skill.add_handler(_bus_handler(site))
    return skill


def make_page_settings(site: str, page_path: str) -> PageSettings:
    """Page settings with the notification bus wired (settings.changed / access.requested).

    Intended for owner-driven settings changes (e.g. the REST /settings path).
    Not used for the default-settings write on page *creation*, which is
    initialization and would be inbox noise.
    """
    settings = PageSettings(site=site, page_path=page_path, storage=_storage)
    settings.add_handler(_bus_handler(site))
    return settings
