#!/usr/bin/env python3
"""Backward-compatible shim for vlm_analysis_contract.

This module has been renamed from ``window_analysis_contract`` to
``vlm_analysis_contract`` as part of the VLM-first architecture migration.

All public symbols are re-exported from the new module for backward
compatibility.  New code should import from ``vlm_analysis_contract`` directly.
"""

from __future__ import annotations

from autocut_core.semantic.vlm_analysis_contract import *  # noqa: F403
from autocut_core.semantic.vlm_analysis_contract import (
    POLICY_VERSION as _NEW_POLICY_VERSION,
    VlmAnalysisContractResult,
    canonicalize_vlm_analysis,
    supports_local_window_media_recovery,
)

# Backward-compatible aliases
WindowAnalysisContractResult = VlmAnalysisContractResult
canonicalize_window_analysis = canonicalize_vlm_analysis
POLICY_VERSION = _NEW_POLICY_VERSION