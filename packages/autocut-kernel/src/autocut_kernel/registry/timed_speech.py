"""The small, authority-only TimedSpeech profile registry boundary.

The important distinction here is deliberate: a resolver names an authority
anchor, while a bootstrap command is the sole place that can turn a verified
registry snapshot into an immutable Store member.  Pipeline/run input has no
way to supply either the payload or the member reference.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from ..media import TimedSpeechProfileRegistryEntry
from ..media.types import canonical_sha256, sha256_prefixed
from ..store.models import (
    ArtifactMember,
    ArtifactScope,
    CommandClaim,
    CommandOutcome,
    CommandSuccess,
    CommittedArtifactMemberReference,
    Job,
)

BOOTSTRAP_TIMED_SPEECH_PROFILE_REGISTRY_COMMAND = "BootstrapTimedSpeechProfileRegistry@2.1.3"
AUTHORITY_BOOTSTRAP_PRINCIPAL = "autocut-authority-bootstrap"
AUTHORITY_BOOTSTRAP_CAPABILITY = "bootstrap_timed_speech_profile_registry"
TIMED_SPEECH_PROFILE_REGISTRY_SCOPE = ArtifactScope(
    "autocut_authority", "registry", "timed_speech_profiles"
)
TIMED_SPEECH_PROFILE_REGISTRY_ARTIFACT_TYPE = "timed_speech_profile_registry_entry"
AUTHORITY_BOOTSTRAP_JOB = Job("autocut_authority", "authority")


class TimedSpeechRegistryError(ValueError):
    """A registry bootstrap, anchor, or resolver invariant did not close."""


def _sha(value: object, name: str) -> str:
    try:
        return sha256_prefixed(value, name)
    except ValueError as error:
        raise TimedSpeechRegistryError(str(error)) from error


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value.strip() or value != value.strip():  # noqa: E721
        raise TimedSpeechRegistryError(f"{name} must be canonical non-empty text")
    return value


@dataclass(frozen=True, slots=True)
class TimedSpeechProfileKey:
    profile_id: str
    profile_version: str

    def __post_init__(self) -> None:
        _text(self.profile_id, "profile_id")
        _text(self.profile_version, "profile_version")
        if "@" in self.profile_id or "/" in self.profile_id or "/" in self.profile_version:
            raise TimedSpeechRegistryError("profile key components are not canonical")

    @property
    def value(self) -> str:
        return f"{self.profile_id}@{self.profile_version}"

    @property
    def logical_id(self) -> str:
        return f"timed-speech/{self.profile_id}/{self.profile_version}"


@dataclass(frozen=True, slots=True)
class AuthorityRegistrySnapshot:
    """A verified compiler output projected into the runtime composition seam."""

    registry_set_sha256: str
    enabled_profile: TimedSpeechProfileKey

    def __post_init__(self) -> None:
        _sha(self.registry_set_sha256, "registry_set_sha256")
        if type(self.enabled_profile) is not TimedSpeechProfileKey:  # noqa: E721
            raise TimedSpeechRegistryError("enabled_profile must be an exact TimedSpeechProfileKey")


# This is intentionally only the authority *locator*, not a profile payload.
# Production still fails closed until the matching anchor/member was created by
# the protected bootstrap command.
DEFAULT_TIMED_SPEECH_AUTHORITY_SNAPSHOT = AuthorityRegistrySnapshot(
    "sha256:0000000000000000000000000000000000000000000000000000000000000000",
    TimedSpeechProfileKey("sensevoice_word_guard_v1", "1"),
)


@dataclass(frozen=True, slots=True)
class AuthorityBootstrapIdentity:
    principal: str
    capability: str

    def __post_init__(self) -> None:
        if (
            self.principal != AUTHORITY_BOOTSTRAP_PRINCIPAL
            or self.capability != AUTHORITY_BOOTSTRAP_CAPABILITY
        ):
            raise TimedSpeechRegistryError("bootstrap identity is not the dedicated authority writer")


@dataclass(frozen=True, slots=True)
class BootstrapTimedSpeechProfileRegistryRequest:
    """Dispatcher-owned bootstrap input; it has no external/caller payload."""

    identity: AuthorityBootstrapIdentity
    snapshot: AuthorityRegistrySnapshot
    entry: TimedSpeechProfileRegistryEntry

    def __post_init__(self) -> None:
        if type(self.identity) is not AuthorityBootstrapIdentity:  # noqa: E721
            raise TimedSpeechRegistryError("bootstrap requires an authority identity")
        if type(self.snapshot) is not AuthorityRegistrySnapshot:  # noqa: E721
            raise TimedSpeechRegistryError("bootstrap requires a verified registry snapshot")
        if type(self.entry) is not TimedSpeechProfileRegistryEntry:  # noqa: E721
            raise TimedSpeechRegistryError("bootstrap requires an exact profile entry")
        if self.key != self.snapshot.enabled_profile:
            raise TimedSpeechRegistryError("compiled snapshot does not enable this profile entry")

    @property
    def key(self) -> TimedSpeechProfileKey:
        return TimedSpeechProfileKey(self.entry.profile_id, self.entry.profile_version)

    @property
    def request_hash(self) -> str:
        return canonical_sha256(
            {
                "command": BOOTSTRAP_TIMED_SPEECH_PROFILE_REGISTRY_COMMAND,
                "profile_key": self.key.value,
                "profile_payload_sha256": self.entry.canonical_hash,
                "registry_set_sha256": self.snapshot.registry_set_sha256,
            }
        )

    @property
    def idempotency_key(self) -> str:
        return "authority-bootstrap:" + canonical_sha256(
            {"profile_key": self.key.value, "registry_set_sha256": self.snapshot.registry_set_sha256}
        )[7:]

    def artifact(self) -> ArtifactMember:
        payload = self.entry.to_mapping()
        payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        return ArtifactMember(
            TIMED_SPEECH_PROFILE_REGISTRY_ARTIFACT_TYPE,
            self.key.logical_id,
            1,
            TIMED_SPEECH_PROFILE_REGISTRY_SCOPE,
            self.entry.canonical_hash,
            payload_json,
        )


@dataclass(frozen=True, slots=True)
class BootstrappedTimedSpeechProfile:
    """The only profile shape a preflight command may receive internally."""

    snapshot: AuthorityRegistrySnapshot
    reference: CommittedArtifactMemberReference
    entry: TimedSpeechProfileRegistryEntry

    def __post_init__(self) -> None:
        if type(self.snapshot) is not AuthorityRegistrySnapshot:  # noqa: E721
            raise TimedSpeechRegistryError("resolved profile lost its authority snapshot")
        if type(self.reference) is not CommittedArtifactMemberReference:  # noqa: E721
            raise TimedSpeechRegistryError("resolved profile lost its immutable member reference")
        if type(self.entry) is not TimedSpeechProfileRegistryEntry:  # noqa: E721
            raise TimedSpeechRegistryError("resolved profile lost its decoded registry entry")
        key = TimedSpeechProfileKey(self.entry.profile_id, self.entry.profile_version)
        if (
            key != self.snapshot.enabled_profile
            or self.reference.scope != TIMED_SPEECH_PROFILE_REGISTRY_SCOPE
            or self.reference.artifact_type != TIMED_SPEECH_PROFILE_REGISTRY_ARTIFACT_TYPE
            or self.reference.logical_id != key.logical_id
            or self.reference.revision != 1
            or self.reference.member_ordinal != 0
            or self.reference.content_hash != self.entry.canonical_hash
        ):
            raise TimedSpeechRegistryError("resolved profile does not close over its authority anchor")


class AuthorityRegistryStore(Protocol):
    def read_bootstrapped_timed_speech_profile(
        self,
        snapshot: AuthorityRegistrySnapshot,
    ) -> BootstrappedTimedSpeechProfile: ...


class TimedSpeechProfileResolver(Protocol):
    @property
    def snapshot(self) -> AuthorityRegistrySnapshot: ...

    def resolve(self, store: AuthorityRegistryStore) -> BootstrappedTimedSpeechProfile: ...


@dataclass(frozen=True, slots=True)
class StoreAnchoredTimedSpeechProfileResolver:
    """Composition-owned resolver which can only dereference an authority anchor."""

    snapshot: AuthorityRegistrySnapshot

    def __post_init__(self) -> None:
        if type(self.snapshot) is not AuthorityRegistrySnapshot:  # noqa: E721
            raise TimedSpeechRegistryError("resolver requires a verified authority snapshot")

    def resolve(self, store: AuthorityRegistryStore) -> BootstrappedTimedSpeechProfile:
        resolved = store.read_bootstrapped_timed_speech_profile(self.snapshot)
        if type(resolved) is not BootstrappedTimedSpeechProfile:  # noqa: E721
            raise TimedSpeechRegistryError("authority Store returned an untyped profile")
        if resolved.snapshot != self.snapshot:
            raise TimedSpeechRegistryError("authority Store returned another registry snapshot")
        return resolved


class AuthorityBootstrapStore(AuthorityRegistryStore, Protocol):
    def claim_command(self, claim: CommandClaim) -> CommandOutcome: ...

    def commit_timed_speech_profile_bootstrap(
        self,
        success: CommandSuccess,
        snapshot: AuthorityRegistrySnapshot,
    ) -> CommandOutcome: ...


class BootstrapTimedSpeechProfileRegistryCommand:
    """The protected writer.  Runtime composition must never invoke this."""

    def __init__(self, store: AuthorityBootstrapStore) -> None:
        self._store = store

    def execute(self, request: BootstrapTimedSpeechProfileRegistryRequest) -> CommandOutcome:
        if type(request) is not BootstrapTimedSpeechProfileRegistryRequest:  # noqa: E721
            raise TimedSpeechRegistryError("bootstrap request must be exact")
        claimed = self._store.claim_command(
            CommandClaim(
                AUTHORITY_BOOTSTRAP_JOB,
                request.idempotency_key,
                BOOTSTRAP_TIMED_SPEECH_PROFILE_REGISTRY_COMMAND,
                request.request_hash,
            )
        )
        if not claimed.is_fresh_claim:
            # Exact-reader validation makes replay fail closed if the durable
            # anchor has been removed or altered after the Receipt was written.
            self._store.read_bootstrapped_timed_speech_profile(request.snapshot)
            return claimed
        artifact = request.artifact()
        set_hash = canonical_sha256(
            [
                {
                    "artifact_type": artifact.artifact_type,
                    "content_hash": artifact.content_hash,
                    "logical_id": artifact.logical_id,
                    "payload_json": json.loads(artifact.payload_json),
                    "revision": artifact.revision,
                    "scope": {
                        "key": artifact.scope.key,
                        "kind": artifact.scope.kind,
                        "namespace": artifact.scope.namespace,
                    },
                }
            ]
        )
        return self._store.commit_timed_speech_profile_bootstrap(
            CommandSuccess(claimed.command_slot_id, set_hash, (artifact,)), request.snapshot
        )
