"""Pipeline stage tools for the auto_cut_bot agent.

Each tool wraps a pipeline stage, exposing its functionality as a
callable agent capability.  The pipeline runs in order:

Source Prep (VLM-First):
  source_windows -> global_context -> vlm_analysis -> confidence_check
  -> event_cards

Series Knowledge:
  episode_digests -> chapter_digests -> series_registry
  -> series_assignment -> series_bible

Story Generation:
  story_catalog -> story_portfolio -> story_treatments
  -> story_scripts -> story_preflight -> story_approval (HUMAN REVIEW)

Plan Orchestration:
  story_evidence -> span_candidates -> story_plans
  -> story_plans_preflight (HUMAN REVIEW) -> story_plans_materialize

QC:
  story_plans_qc_admission (HUMAN REVIEW) -> story_qc -> story_qc_review (HUMAN REVIEW)

Render:
  story_render
"""

from auto_cut_bot.agent.tools.pipeline.source_windows import SourceWindowsTool
from auto_cut_bot.agent.tools.pipeline.source_transcripts import SourceTranscriptsTool
from auto_cut_bot.agent.tools.pipeline.global_context import GlobalContextTool
from auto_cut_bot.agent.tools.pipeline.window_analysis import WindowAnalysisTool
from auto_cut_bot.agent.tools.pipeline.confidence_check import ConfidenceCheckTool
from auto_cut_bot.agent.tools.pipeline.event_cards import EventCardsTool
from auto_cut_bot.agent.tools.pipeline.episode_digests import EpisodeDigestsTool
from auto_cut_bot.agent.tools.pipeline.chapter_digests import ChapterDigestsTool
from auto_cut_bot.agent.tools.pipeline.series_registry import SeriesRegistryTool
from auto_cut_bot.agent.tools.pipeline.series_assignment import SeriesAssignmentTool
from auto_cut_bot.agent.tools.pipeline.series_bible import SeriesBibleTool
from auto_cut_bot.agent.tools.pipeline.story_catalog import StoryCatalogTool
from auto_cut_bot.agent.tools.pipeline.story_portfolio import StoryPortfolioTool
from auto_cut_bot.agent.tools.pipeline.story_treatments import StoryTreatmentsTool
from auto_cut_bot.agent.tools.pipeline.story_scripts import StoryScriptsTool
from auto_cut_bot.agent.tools.pipeline.story_preflight import StoryPreflightTool
from auto_cut_bot.agent.tools.pipeline.story_approval import StoryApprovalTool
from auto_cut_bot.agent.tools.pipeline.story_evidence import StoryEvidenceTool
from auto_cut_bot.agent.tools.pipeline.span_candidates import SpanCandidatesTool
from auto_cut_bot.agent.tools.pipeline.story_plans import StoryPlansTool
from auto_cut_bot.agent.tools.pipeline.story_plans_preflight import StoryPlansPreflightTool
from auto_cut_bot.agent.tools.pipeline.story_plans_materialize import StoryPlansMaterializeTool
from auto_cut_bot.agent.tools.pipeline.story_qc import StoryQCTool
from auto_cut_bot.agent.tools.pipeline.story_qc_review import StoryQCReviewTool
from auto_cut_bot.agent.tools.pipeline.story_plans_qc_admission import StoryPlansQCAdmissionTool
from auto_cut_bot.agent.tools.pipeline.story_render import StoryRenderTool
from auto_cut_bot.agent.tools.pipeline.database_write import DatabaseWriteTool
from auto_cut_bot.agent.tools.pipeline.db_query import DBQueryTool
from auto_cut_bot.agent.tools.pipeline.domain_source_agent import DomainSourceAgentTool
from auto_cut_bot.agent.tools.pipeline.domain_story_agent import DomainStoryAgentTool
from auto_cut_bot.agent.tools.pipeline.domain_production_agent import DomainProductionAgentTool

__all__ = [
    # Source Prep
    "SourceWindowsTool",
    "SourceTranscriptsTool",
    "GlobalContextTool",
    "WindowAnalysisTool",
    "ConfidenceCheckTool",
    "EventCardsTool",
    # Series Knowledge
    "EpisodeDigestsTool",
    "ChapterDigestsTool",
    "SeriesRegistryTool",
    "SeriesAssignmentTool",
    "SeriesBibleTool",
    # Story Generation
    "StoryCatalogTool",
    "StoryPortfolioTool",
    "StoryTreatmentsTool",
    "StoryScriptsTool",
    "StoryPreflightTool",
    "StoryApprovalTool",
    # Plan Orchestration
    "StoryEvidenceTool",
    "SpanCandidatesTool",
    "StoryPlansTool",
    "StoryPlansPreflightTool",
    "StoryPlansMaterializeTool",
    # QC
    "StoryQCTool",
    "StoryQCReviewTool",
    "StoryPlansQCAdmissionTool",
    # Render
    "StoryRenderTool",
    "DatabaseWriteTool",
    # DB Query (read-only)
    "DBQueryTool",
    # Domain Agents
    "DomainSourceAgentTool",
    "DomainStoryAgentTool",
    "DomainProductionAgentTool",
]

# Registered pipeline tools for ToolLoader auto-discovery
ALL_PIPELINE_TOOLS = [
    SourceWindowsTool, SourceTranscriptsTool, GlobalContextTool, WindowAnalysisTool,
    ConfidenceCheckTool, EventCardsTool,
    EpisodeDigestsTool, ChapterDigestsTool, SeriesRegistryTool,
    SeriesAssignmentTool, SeriesBibleTool, StoryCatalogTool,
    StoryPortfolioTool, StoryTreatmentsTool, StoryScriptsTool,
    StoryPreflightTool, StoryApprovalTool, StoryEvidenceTool,
    SpanCandidatesTool, StoryPlansTool, StoryPlansPreflightTool, StoryPlansMaterializeTool,
    StoryQCTool, StoryQCReviewTool, StoryPlansQCAdmissionTool, StoryRenderTool,
    DatabaseWriteTool, DBQueryTool,
    DomainSourceAgentTool, DomainStoryAgentTool, DomainProductionAgentTool,
]