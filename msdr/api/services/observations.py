"""Signal detail panel (U3) and observations / honest evidence (S2).

AppService mixin: methods moved verbatim from msdr/api/service.py;
all attributes they use are initialised in AppService.__init__.
"""

import time

from fastapi import HTTPException

from msdr import __version__
from msdr.api.signal_detail import build_signal_detail
from msdr.contracts.evidence import Claim
from msdr.library.models import Signal


class ObservationServiceMixin:
    # --- signal detail panel (U3) -------------------------------------------

    def signal_detail(
        self,
        *,
        frequency_hz: int | None = None,
        signal_id: str | None = None,
        find_id: str | None = None,
    ) -> dict:
        """GET /api/signal-detail: aggregate memory/find/survey-label/evidence
        for one frequency -- see msdr/api/signal_detail.py."""
        return build_signal_detail(
            self, frequency_hz=frequency_hz, signal_id=signal_id, find_id=find_id
        )

    # --- observations and honest evidence (S2) -------------------------------

    def observations_list(
        self,
        *,
        frequency_hz: int | None = None,
        tolerance_hz: int = 6000,
        signal_id: str | None = None,
        session_id: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        obs = self.observations.list(
            frequency_hz=frequency_hz,
            tolerance_hz=tolerance_hz,
            signal_id=signal_id,
            session_id=session_id,
            limit=limit,
        )
        return [self.observations.summary(o) for o in obs]

    def _get_observation_or_404(self, observation_id: str):
        obs = self.observations.get(observation_id)
        if obs is None:
            raise HTTPException(404, "observation not found")
        return obs

    def observation_get(self, observation_id: str) -> dict:
        return self.observations.summary(self._get_observation_or_404(observation_id))

    def observation_patch(
        self,
        observation_id: str,
        *,
        signal_id: str | None = None,
        notes: str | None = None,
        fields: set[str] | frozenset[str] = frozenset({"signal_id"}),
    ) -> dict:
        """`fields` names the keys the PATCH body actually carried, so a
        notes-only edit never unlinks the memory (and vice versa)."""
        self._get_observation_or_404(observation_id)
        if "signal_id" in fields:
            self.observations.link_signal(observation_id, signal_id)
        if "notes" in fields and notes is not None:
            self.observations.set_notes(observation_id, notes)
        updated = self.observations.get(observation_id)
        return {
            **self.observations.summary(updated),
            "note": "link suggests, does not prove identity",
        }

    def observation_promote(self, observation_id: str, *, name: str | None = None) -> dict:
        """POST /api/observations/{id}/promote: a memory (Signal) from an
        observation, linked back to it -- mirrors finds_promote. Decoders come
        only from current validated_decode claims by a decoder (not RDS,
        not unchecked decoder_output)."""
        obs = self._get_observation_or_404(observation_id)
        frequency_hz = obs.measured_hz or obs.reported_hz
        if frequency_hz is None:
            raise HTTPException(409, "observation has no frequency to promote")
        receiver = obs.receiver or {}
        decoders: list[str] = []
        for claim in self.observations.claims(observation_id, include_historical=False):
            if claim.evidence_type == "validated_decode" and claim.producer.startswith("decoder."):
                decoder_id = claim.producer.removeprefix("decoder.")
                if decoder_id and decoder_id not in decoders:
                    decoders.append(decoder_id)
        demodulator = receiver.get("demodulator")
        bandwidth = obs.bandwidth_hz or receiver.get("filter_bandwidth")
        signal = Signal(
            name=name or f"{frequency_hz / 1e6:.4f} observation",
            status="unknown",
            frequency_hz=int(frequency_hz),
            demodulator=demodulator or "NFM",
            filter_bandwidth_hz=int(bandwidth) if bandwidth else 12_500,
            decoders=decoders,
            # receiver is a session snapshot keyed by wire property names
            # (see AppService._session_receiver_snapshot) -- "active_antenna",
            # not the Signal field name. None when the observation predates
            # this snapshot or never had one (legacy import, recorder
            # bookmarks that only carry demodulator).
            antenna=receiver.get("active_antenna"),
            lna_state=receiver.get("lna_state"),
            notes=obs.notes,
        )
        self.create_signal(signal)
        self.observations.link_signal(observation_id, signal.id)
        updated = self.observations.get(observation_id)
        return {
            "signal": signal.model_dump(mode="json"),
            "observation": self.observations.summary(updated),
            "note": "link suggests, does not prove identity",
        }

    def _observation_export(self, obs) -> dict:
        session = self.observations.get_session(obs.session_id) if obs.session_id else None
        return {
            "observation": obs.model_dump(mode="json"),
            "claims": [
                {**c.model_dump(mode="json"), "historical": c.historical}
                for c in self.observations.claims(obs.id, include_historical=True)
            ],
            "refs": [r.model_dump(mode="json") for r in self.observations.refs(obs.id)],
            "decode_events": self.observations.decode_events(obs.id),
            "session": session.model_dump(mode="json") if session is not None else None,
            "exported_at": time.time(),
            "msdr_version": __version__,
        }

    def observation_export(self, observation_id: str) -> dict:
        return self._observation_export(self._get_observation_or_404(observation_id))

    def observations_export(self, *, since_ts: float | None = None, limit: int = 500) -> list[dict]:
        limit = max(1, min(int(limit), 5000))
        return [
            self._observation_export(o)
            for o in self.observations.list_updated_since(since_ts, limit)
        ]

    def observation_add_claim(
        self,
        observation_id: str,
        *,
        subject: str,
        value: str,
        rationale: str,
        supersedes: str | None = None,
    ) -> dict:
        self._get_observation_or_404(observation_id)
        claim = Claim(
            observation_id=observation_id,
            subject=subject,
            value=value,
            evidence_type="operator",
            integrity="not_applicable",
            producer="operator",
            support=[rationale],
        )
        if supersedes:
            try:
                self.observations.supersede(supersedes, claim)
            except KeyError:
                raise HTTPException(404, "claim not found") from None
        else:
            self.observations.add_claim(claim)
        updated = self.observations.get(observation_id)
        return self.observations.summary(updated)
