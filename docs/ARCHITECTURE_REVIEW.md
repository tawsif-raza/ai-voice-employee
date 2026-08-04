# Architecture Review — docs/ARCHITECTURE.md

**Reviewed:** `docs/ARCHITECTURE.md` (all 10 sections)
**Status:** Review only. No changes have been made to ARCHITECTURE.md or any other file. Awaiting approval before applying any of the below.

---

## 1. Missing components

| # | Finding | Where | Severity |
|---|---|---|---|
| 1.1 | **No Speech-to-Text (STT) component.** The system is described as "voice-first," but nothing shows how spoken input becomes the text the Conversation Manager receives. It's not in the component diagram, the responsibilities table, or the communication rules. | Doc-wide; most visible in §1, §4, §5 | High |
| 1.2 | **No Text-to-Speech (TTS) / audio synthesis component.** Same gap on the output side — the voice client is treated as a generic client, but audio synthesis (a real, separate concern with its own external dependency) isn't represented anywhere. | §4, §5 | High |
| 1.3 | **Configuration store isn't a component.** §3 (Core Principle 6) leans on "configuration over code" as an architectural principle, and §7's deployment view mounts a config volume, but no config/rules store appears as a component in the §4 logical diagram or the §5 responsibilities table. A principle the document relies on has no corresponding box. | §3, §4, §5, §7 | Medium |
| 1.4 | **No reverse proxy / load balancer / API gateway component**, despite §9 explicitly assuming one exists ("TLS termination is assumed to be handled by infrastructure in front of the API") and §8 assuming horizontal scaling behind "a load balancer." Neither appears in the §7 deployment diagram, which shows the client hitting the API container directly. | §7 (diagram vs. prose), §8, §9 | Medium |
| 1.5 | **Training Pipeline and Evaluation Harness are documented in §5 but absent from the §4 diagram.** §7's deployment view does show a trainer container, so the omission is specific to the logical component diagram. If §4 is meant to be request-path-only, that scope limitation is never stated, so a reader can't tell whether the omission is intentional. | §4 vs. §5/§7 | Medium |
| 1.6 | **No explicit "conversation history" contract/component.** The architecture depends on history being caller-supplied per request (§8), but there's no component, store, or even a labeled data contract for it — it's implied, not documented. | §8, §9 | Low |

## 2. Circular dependencies

| # | Finding | Where | Severity |
|---|---|---|---|
| 2.1 | **API Layer ↔ Conversation Manager is drawn as a two-node cycle.** The diagram has both `Server --> CM` (line 77) and `CM --> Server` (line 89). As drawn, this is a literal circular dependency between two components. In reality this almost certainly represents "request in, response out" (control flow / return value), not a structural dependency — but the diagram doesn't distinguish call direction from return direction, so it reads as a cycle to anyone doing dependency analysis on the graph, and would trip a naive cycle-detector. | §4 diagram, lines 77 & 89 | Medium |
| 2.2 | **No legend distinguishes edge semantics**, which is part of why 2.1 is ambiguous. Dotted edges are used for at least two different meanings — a future/not-yet-built relationship (`CM -.mediates all access, future.-> BizAPI`) and a static build-time relationship (`FAISSIndex -.built from.-> KB`) — with no key explaining that. A reader can't tell "dotted" means "temporal/future" in one place and "derivation" in another. | §4 diagram | Low |

No other cycles were found — the rest of the graph (CM → {ClinicalGuard, Retriever, LLM, HandoffDetector}, Retriever → Embedder/FAISSIndex, FAISSIndex → KB) is a clean DAG.

## 3. Tight coupling

| # | Finding | Where | Severity |
|---|---|---|---|
| 3.1 | **Conversation Manager and Model Layer are the same physical module today**, per §5's own admission ("implemented inline inside the inference component"). This directly undercuts Core Principle 7 (§3): "every component is independently testable... without requiring a full model load." As currently implemented, testing the orchestration sequence (safety-check ordering, retrieval-before-generation) in isolation *does* require loading the model, because they're the same code. The document states the principle and the contradicting fact in different sections without reconciling them. | §3 (Principle 7) vs. §5 | High |
| 3.2 | **Handoff Detector and Clinical Safety Guard share a single underlying detection engine** (confirmed by the actual codebase — one generic layered matcher, configured by two different files), but the document presents them in §4/§5/§6 as two independent components with no relationship noted. This matters because it's a real shared-fate risk: a change to the shared matching engine — intended to fix or extend handoff detection — can silently change clinical-escalation behavior too, and vice versa. This is exactly the kind of coupling a security/architecture review should surface, and §9 (Security Considerations) is silent on it despite calling the Clinical Safety Guard "a security-relevant control." | §4, §5, §6, §9 | High |
| 3.3 | **Model Layer, Conversation Manager, and Knowledge Layer are co-located in one process** (§7: "the API container runs the Conversation Manager's orchestration logic, the Model Layer, and the Knowledge Layer in a single process"). §7 states this as fact but never frames it as a coupling trade-off; §8 (Scalability) only mentions retrieval extraction as a *future* option, without naming today's co-location as a present architectural risk (e.g., a memory leak or crash in retrieval takes down generation too, since they share a process/failure domain). | §7, §8 | Medium |
| 3.4 | **Knowledge Base and Vector Index freshness coupling isn't risk-assessed.** §5 notes the index is "rebuilt from the Knowledge Base when stale," which implies a window where they can disagree. The document doesn't discuss what happens if a query is served against a stale index (e.g., wrong/outdated retrieved facts) or who/what guarantees the rebuild happens. | §5 | Low |

## 4. Scalability issues

| # | Finding | Where | Severity |
|---|---|---|---|
| 4.1 | **Full-history-per-request has an unaddressed cost-scaling problem.** §8 praises caller-supplied history for enabling stateless horizontal scaling, but doesn't mention the flip side: every turn re-sends and re-processes the *entire* conversation history, so per-turn latency/compute grows with conversation length, with no truncation, summarization, or windowing strategy discussed anywhere in the document. | §8 | High |
| 4.2 | **"Scale by adding replicas" (§8) ignores per-replica resource cost.** Each replica must independently load the full LLM, the embedding model, and the FAISS index into memory (per §7's single-process co-location). §8 doesn't quantify or even acknowledge that horizontal scaling here means multiplying a non-trivial memory/startup-time cost per instance, which is a real constraint on how "simple" that scaling story actually is. | §7, §8 | Medium |
| 4.3 | **No discussion of concurrency within a single replica.** The document doesn't address whether one API process can serve multiple in-flight generations concurrently, or whether requests queue/serialize behind a single loaded model instance. This is a first-order scalability question for a generation-heavy service and isn't mentioned. | §8 | Medium |
| 4.4 | **No backpressure/queueing strategy for burst load** is mentioned — what happens when concurrent demand exceeds capacity (reject, queue, degrade) is left undefined. | §8 | Medium |
| 4.5 | **Future Business Action Layer's scalability/availability impact isn't considered.** §10 introduces it, but §8 never revisits scalability in light of it — once the Conversation Manager depends on external business systems, their latency/availability becomes part of the platform's critical path, which is a forward-looking scalability (and reliability) concern worth flagging now rather than after the fact. | §8 vs. §10 | Low |

## 5. Security concerns

| # | Finding | Where | Severity |
|---|---|---|---|
| 5.1 | **No mention of prompt-injection risk from user input.** §9 covers retrieved knowledge-base content being treated as "data, not instructions," but says nothing about adversarial *user* input designed to override system instructions, talk the model out of a clinical/handoff escalation, or manipulate response framing. For a system whose safety model partly depends on the LLM's own in-band behavior (handoff phrasing is detected from model output, per §6 rule 4), this is a meaningful gap. | §9 | High |
| 5.2 | **No rate limiting / resource-exhaustion consideration.** §9 flags missing authN/authZ and missing transport security, but never mentions the absence of rate limiting or protection against resource-exhaustion (e.g., large/repeated requests exhausting CPU given generation is already the latency bottleneck per §8). | §9 | Medium |
| 5.3 | **Knowledge base integrity/access control isn't addressed for the medicine domain specifically.** §9 says content changes "should go through the same review discipline as code changes," but doesn't address *who can write to it*, whether that's enforced technically (vs. just a process norm), or what the blast radius is if the medicine-domain content is wrong or tampered with — a materially higher-stakes failure mode than a wrong FAQ answer. | §9 | Medium |
| 5.4 | **No mention of conversation-content logging/PII exposure risk**, even though user turns could contain health-related statements (the platform has a medicine domain). §9 states no PII/PHI is *persisted by the platform*, but doesn't address whether conversation content ends up in logs, error traces, or evaluation artifacts as a side effect of operating the system. | §9 | Medium |
| 5.5 | **No model/dependency supply-chain consideration.** The LLM and embedding model are pulled from an external source at some point in the lifecycle; the document doesn't mention version pinning, integrity verification, or provenance risk for either. | §5, §9 | Low |
| 5.6 | **Future Business Action Layer's security model is undefined.** §10 introduces it with no forward reference to how the Conversation Manager will authenticate to/scope credentials for business systems — worth at least a placeholder note given §3/§6 make a point of the LLM never reaching it directly, but say nothing about how the Conversation Manager itself will do so safely. | §9, §10 | Low |

## 6. Naming inconsistencies

| # | Finding | Where | Severity |
|---|---|---|---|
| 6.1 | **"Vector Index" vs. "FAISS Index."** The same component is called `FAISSIndex`/"FAISS Index" in the §4 diagram, but "Vector Index" in the §5 table and §6 (rule 6, "Knowledge Base and Vector Index"). Pick one canonical name. | §4, §5, §6 | Medium |
| 6.2 | **"Business Action Layer" vs. "Business APIs" / "business API."** §4's diagram subgraph and §5's table both name the component "Business Action Layer," but the node inside the diagram is labeled "Business APIs," and §6 rule 7 shifts to lowercase singular "business API" mid-sentence. It's never explicitly stated that "Business Action Layer" is the mediating component and "business APIs" are the external systems it fronts — a reader has to infer that distinction. | §4, §5, §6 | Medium |
| 6.3 | **"Model Layer" vs. "the inference component" vs. "model-loading code."** These appear to refer to the same real code area but are named three different ways across §4/§7, §5, and §10 respectively, with nothing stating they're the same thing. | §4, §5, §7, §10 | Medium |
| 6.4 | **"Knowledge Layer" is defined only in the diagram and never used in prose.** §4 introduces "Knowledge Layer" as the subgraph grouping Retriever/Embedder/Index/KB, but §5 through §9 only ever say "Knowledge Base" (sometimes seemingly meaning just the raw data, sometimes reading as if it includes the index) — the umbrella term from the diagram is never reused or defined in text. | §4 vs. §5–§9 | Low |
| 6.5 | **Field-level vs. descriptive naming for the same output.** §1 describes the response payload in prose ("handoff flag/confidence, retrieved sources, clinical-guard status"), while the §4 diagram's edge label uses literal field-style names ("response + is_handoff + retrieved_chunks"). Mixing human-readable and code-identifier-style naming for the same concept, without noting they refer to the same thing, reads as inconsistent. | §1 vs. §4 | Low |
| 6.6 | **Capitalization of "API Layer" / "API layer" is inconsistent** (title case in §4/§5, sentence case in §6). Cosmetic, but worth a pass for consistency given the rest of the document is disciplined about bolding/naming components. | §4, §5, §6 | Low |

---

## Summary

| Category | High | Medium | Low | Total |
|---|---|---|---|---|
| Missing components | 2 | 3 | 1 | 6 |
| Circular dependencies | 0 | 1 | 1 | 2 |
| Tight coupling | 2 | 2 | 0 | 4 |
| Scalability issues | 1 | 3 | 1 | 5 |
| Security concerns | 1 | 3 | 2 | 6 |
| Naming inconsistencies | 0 | 3 | 3 | 6 |

**Highest-priority items** (recommend addressing first, pending your approval):
- **3.1** — the stated "independently testable" principle is currently false in practice; either soften the principle's wording or flag it as a target-state aspiration, not a current guarantee.
- **3.2** — the shared handoff/clinical-guard engine is an undocumented shared-fate risk between a safety-critical control and a UX feature; this belongs in Security Considerations, not just an implementation detail.
- **1.1 / 1.2** — STT/TTS are core to a "voice-first" platform and are entirely absent from the architecture.
- **4.1** — unbounded history growth per request is a real cost/latency scaling problem with no mitigation discussed.
- **5.1** — prompt-injection from user input is unaddressed despite the platform's safety model partly depending on the LLM's own output being trustworthy enough to scan.

No changes have been made. Let me know which of the above you'd like applied, and I'll update `docs/ARCHITECTURE.md` accordingly.
