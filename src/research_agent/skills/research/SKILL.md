---
name: research
description: Resolve dynamically prioritized product questions using public HTML evidence.
---

Continue as the same single agent. Review and order the saved questions initially.
Select important open questions that materially improve understanding or unlock
other answers. Save the active question with update_questions before investigating.
Reassess priorities as evidence arrives; reorder, add, refine, merge, reopen, or
switch questions when appropriate. Keep stable IDs for retained questions.

The active question guides source selection; it does not restrict what the agent
can learn. After reading a source, consider its implications for all questions.
Update or answer any question the evidence informs, including through clearly
labeled inference. A question does not need to become active before it can be
answered. Record useful evidence against each affected question and reset its
unproductive-page streak. Do not increment other questions' streaks merely because
a page read for the active question did not address them.

Use query_site_index: terms match any term across URL/title/description, ranked by
matched terms then URL; required_phrase and url_contains can narrow results. Empty
search browses. Read selected HTML pages. Company pages must be indexed or linked
from saved evidence; external sources must be linked from company evidence. Do not
use arbitrary URLs, external search, PDF/media processing, or additional agents.

Cite supported claims with [S1] short source IDs. Distinguish vendor claims from
established behavior. Answers may be documented, inferred, or mixed. Explicitly
labeled inference grounded in cited evidence with a brief explanation can answer a
question. Important unsupported assumptions remain questions. Revise the profile
and priorities when findings change understanding, not after every page. Reuse
existing context instead of fetching the same evidence again.

Save the full ordered index with update_questions, preserving prior assessments.
Assess only relevant successfully read sources: useful means evidence contributed
information for that question. Code counts distinct unproductive pages and resets
on useful evidence. Failed/blocked reads and cached rereads do not advance progress.
Three consecutive distinct relevant unproductive pages permit needs_interview.
Exhausted plausible public leads permit earlier deferral with an explanation in
closure_reason. Say "unresolved through public research", never that no public
information exists. Later evidence may reopen or answer deferred questions.

Finish with finish_research containing the final profile, full question_index, and
reason. There must be no open questions. Interview questions are the needs_interview
subset of this index, not a separate store. Budget or context exhaustion leaves the
run incomplete; it never justifies automatically deferring questions or finishing.
All exchanges are retained without compaction or silent truncation.
