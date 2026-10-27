"""The pack wire shape and its value objects (§22.9).

``LLM/tender_pack_resolution_openapi.yaml`` is not in this repository. The shape below is the
assumed contract (and the cache's storage form): Tender returns exactly this document.
"""

from __future__ import annotations

from typing import Any

from rag.domain.packs.generation import (
    ContentRule,
    EligibilityDecl,
    ExtractionSchema,
    FieldSpec,
)
from rag.domain.packs.profile import (
    DomainProfile,
    LanguagePack,
    PackBundle,
    PackSource,
    SectionTypeProfile,
)
from rag.domain.retrieval.sufficiency import RequiredInputRule, SufficiencyThreshold
from rag.domain.shared.enums import EligibilityState


def _section(section_type: str, d: dict[str, Any]) -> SectionTypeProfile:
    s = d["sufficiency"]
    return SectionTypeProfile(
        section_type=section_type,
        candidate_depth=int(d["candidate_depth"]),
        rerank_in=int(d["rerank_in"]),
        rerank_out=int(d["rerank_out"]),
        sufficiency=SufficiencyThreshold(
            float(s["min_top_score"]), int(s["min_chunks"]), float(s["chunk_score_floor"])
        ),
        diversity_bound=int(d["diversity_bound"]),
        values_provisional=bool(d.get("values_provisional", True)),
        vocabulary=tuple(d.get("vocabulary", ())),
        mandatory_clauses=frozenset(d.get("mandatory_clauses", ())),
        preferred_roles=tuple(d.get("preferred_roles", ())),
        skeleton=str(d.get("skeleton", "")),
        compliance=bool(d.get("compliance", False)),
        failure_policy=d.get("failure_policy"),
        disclaimer_ref=d.get("disclaimer_ref"),
    )


def _eligibility(d: dict[str, Any]) -> EligibilityDecl:
    return EligibilityDecl(EligibilityState(d.get("state", "GENERATABLE")), d.get("reason"))


def _schema(ref: str, d: dict[str, Any]) -> ExtractionSchema:
    return ExtractionSchema(
        ref=ref,
        version=str(d["version"]),
        description=str(d.get("description", "")),
        fields=tuple(
            FieldSpec(
                name=str(f["name"]),
                type=str(f.get("type", "string")),
                description=str(f.get("description", "")),
                pattern=f.get("pattern"),
                minimum=None if f.get("minimum") is None else float(f["minimum"]),
                maximum=None if f.get("maximum") is None else float(f["maximum"]),
                choices=tuple(f.get("choices", ())),
                conditional=bool(f.get("conditional", False)),
            )
            for f in d.get("fields", ())
        ),
    )


def bundle_from_dict(body: dict[str, Any]) -> PackBundle:
    """Parse a pack document. ``KeyError``/``ValueError``/``TypeError`` where it is malformed."""
    dp, lp = body["domain_profile"], body["language_pack"]
    profile = DomainProfile(
        ref=str(dp["ref"]),
        version=str(dp["version"]),
        taxonomy_version=str(dp["taxonomy_version"]),
        default_profile=_section("*", dp["default_profile"]),
        section_profiles={k: _section(k, v) for k, v in dp.get("section_profiles", {}).items()},
        required_inputs={
            k: RequiredInputRule(
                tuple(v.get("document_roles", ())),
                tuple(v.get("resolvable_entities", ())),
                bool(v.get("non_empty_collections", False)),
            )
            for k, v in dp.get("required_inputs", {}).items()
        },
        document_roles=tuple(dp.get("document_roles", ())),
        entity_keys=tuple(dp.get("entity_keys", ())),
        skeleton_tokens=int(dp.get("skeleton_tokens", 0)),
        answer_skeleton=str(dp.get("answer_skeleton", "")),
        tenant_overrides=str(dp.get("tenant_overrides", "")),
        outline_gate_required=bool(dp.get("outline_gate_required", False)),
        eligibility={k: _eligibility(v) for k, v in dp.get("eligibility", {}).items()},
        default_eligibility=_eligibility(dp.get("default_eligibility", {})),
        generation_model_profile=dp.get("generation_model_profile"),
        judge_model_profile=dp.get("judge_model_profile"),
        content_rules=tuple(
            ContentRule(str(r["name"]), str(r["pattern"]), tuple(r.get("layers", ())))
            for r in dp.get("content_rules", ())
        ),
        extraction_schemas={k: _schema(k, v) for k, v in dp.get("extraction_schemas", {}).items()},
    )
    pack = LanguagePack(
        language=str(lp["language"]),
        version=str(lp["version"]),
        terminology={k: tuple(v) for k, v in lp.get("terminology", {}).items()},
    )
    return PackBundle(profile, pack, PackSource.TENDER)


def _section_dict(p: SectionTypeProfile) -> dict[str, Any]:
    return {
        "candidate_depth": p.candidate_depth,
        "rerank_in": p.rerank_in,
        "rerank_out": p.rerank_out,
        "sufficiency": {
            "min_top_score": p.sufficiency.min_top_score,
            "min_chunks": p.sufficiency.min_chunks,
            "chunk_score_floor": p.sufficiency.chunk_score_floor,
        },
        "diversity_bound": p.diversity_bound,
        "values_provisional": p.values_provisional,
        "vocabulary": list(p.vocabulary),
        "mandatory_clauses": sorted(p.mandatory_clauses),
        "preferred_roles": list(p.preferred_roles),
        "skeleton": p.skeleton,
        "compliance": p.compliance,
        "failure_policy": p.failure_policy,
        "disclaimer_ref": p.disclaimer_ref,
    }


def _eligibility_dict(e: EligibilityDecl) -> dict[str, Any]:
    return {"state": e.state.value, "reason": e.reason}


def _schema_dict(x: ExtractionSchema) -> dict[str, Any]:
    return {
        "version": x.version,
        "description": x.description,
        "fields": [
            {
                "name": f.name,
                "type": f.type,
                "description": f.description,
                "pattern": f.pattern,
                "minimum": f.minimum,
                "maximum": f.maximum,
                "choices": list(f.choices),
                "conditional": f.conditional,
            }
            for f in x.fields
        ],
    }


def bundle_to_dict(bundle: PackBundle) -> dict[str, Any]:
    """The inverse of ``bundle_from_dict`` (the cache's storage form)."""
    dp, lp = bundle.domain_profile, bundle.language_pack
    return {
        "domain_profile": {
            "ref": dp.ref,
            "version": dp.version,
            "taxonomy_version": dp.taxonomy_version,
            "default_profile": _section_dict(dp.default_profile),
            "section_profiles": {k: _section_dict(v) for k, v in dp.section_profiles.items()},
            "required_inputs": {
                k: {
                    "document_roles": list(r.document_roles),
                    "resolvable_entities": list(r.resolvable_entities),
                    "non_empty_collections": r.non_empty_collections,
                }
                for k, r in dp.required_inputs.items()
            },
            "document_roles": list(dp.document_roles),
            "entity_keys": list(dp.entity_keys),
            "skeleton_tokens": dp.skeleton_tokens,
            "answer_skeleton": dp.answer_skeleton,
            "tenant_overrides": dp.tenant_overrides,
            "outline_gate_required": dp.outline_gate_required,
            "eligibility": {k: _eligibility_dict(v) for k, v in dp.eligibility.items()},
            "default_eligibility": _eligibility_dict(dp.default_eligibility),
            "generation_model_profile": dp.generation_model_profile,
            "judge_model_profile": dp.judge_model_profile,
            "content_rules": [
                {"name": r.name, "pattern": r.pattern, "layers": list(r.layers)}
                for r in dp.content_rules
            ],
            "extraction_schemas": {k: _schema_dict(v) for k, v in dp.extraction_schemas.items()},
        },
        "language_pack": {
            "language": lp.language,
            "version": lp.version,
            "terminology": {k: list(v) for k, v in lp.terminology.items()},
        },
    }
