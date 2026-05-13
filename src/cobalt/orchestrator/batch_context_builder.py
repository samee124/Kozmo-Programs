"""Batch context builder — assembles BatchContext from candidates and docs."""

import logging

from cobalt.agents.research_agent import ResearchAgent
from cobalt.brain.loader import load_brain
from cobalt.intake._cleaner import structural_clean
from cobalt.intake._normalizer import normalize
from cobalt.models.schemas.signal_profile_schema import BatchContext
from cobalt.tools.source_intake import DocRecord, RawCandidate

logger = logging.getLogger(__name__)


def build_batch_context(
    raw_candidates: list[RawCandidate],
    research_agent: ResearchAgent,
    programme_id: str,
    doc_registry: dict[str, DocRecord],
) -> BatchContext:
    """Assemble BatchContext from raw candidates, ERP/AP scans, and documents. Never raises."""
    logger.info("Building batch context for %d candidates", len(raw_candidates))
    try:
        # Step 1 — load brain
        try:
            brain = load_brain()
            known_vendors = brain.known_vendors
            rebrand_map = brain.rebrand_map
            alias_map = brain.alias_map
            logger.info(
                "Brain loaded — %d known vendors  %d rebrands  %d aliases",
                len(known_vendors), len(rebrand_map), len(alias_map),
            )
        except Exception as exc:
            logger.warning("load_brain failed in build_batch_context: %s", exc)
            known_vendors = {}
            rebrand_map = {}
            alias_map = {}

        # Step 2 — build all_keys
        all_keys: list[str] = []
        for candidate in raw_candidates:
            try:
                _, key = normalize(structural_clean(candidate.raw_name))
                all_keys.append(key)
            except Exception as exc:
                logger.warning("normalize failed for %r: %s", candidate.raw_name, exc)

        # Step 3 — ERP batch scan
        try:
            erp_hits = research_agent.erp_batch_scan(all_keys)
            logger.info("ERP scan — %d hits out of %d candidates", len(erp_hits), len(all_keys))
        except Exception as exc:
            logger.warning("erp_batch_scan failed: %s", exc)
            erp_hits = {}

        # Step 4 — AP batch scan
        try:
            ap_counts = research_agent.ap_batch_scan(all_keys)
            logger.info("AP scan  — %d hits out of %d candidates", len(ap_counts), len(all_keys))
        except Exception as exc:
            logger.warning("ap_batch_scan failed: %s", exc)
            ap_counts = {}

        # Step 5 — build doc_registry {comparison_key: [doc_id, ...]}
        doc_registry_ctx: dict[str, list[str]] = {}
        for doc_id, doc_record in doc_registry.items():
            if doc_record.vendor_key:
                doc_registry_ctx.setdefault(doc_record.vendor_key, []).append(doc_id)

        # Step 6 — build candidate_hints {comparison_key: {spend_hint, category_hint, extra_fields}}
        candidate_hints: dict[str, dict] = {}
        for candidate in raw_candidates:
            if candidate.spend_hint is None and candidate.category_hint is None and not candidate.extra_fields:
                continue
            try:
                _, key = normalize(structural_clean(candidate.raw_name))
                candidate_hints[key] = {
                    "spend_hint": candidate.spend_hint,
                    "category_hint": candidate.category_hint,
                    "extra_fields": candidate.extra_fields,
                }
            except Exception as exc:
                logger.warning("candidate_hints build failed for %r: %s", candidate.raw_name, exc)

        return BatchContext(
            known_vendors=known_vendors,
            rebrand_map=rebrand_map,
            alias_map=alias_map,
            erp_hits=erp_hits,
            ap_counts=ap_counts,
            all_keys=all_keys,
            doc_registry=doc_registry_ctx,
            candidate_hints=candidate_hints,
        )

    except Exception as exc:
        logger.error("build_batch_context failed unexpectedly: %s", exc)
        return BatchContext(
            known_vendors={},
            rebrand_map={},
            alias_map={},
            erp_hits={},
            ap_counts={},
            all_keys=[],
            doc_registry={},
            candidate_hints={},
        )
