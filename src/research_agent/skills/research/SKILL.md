---
name: research
description: Develop product understanding through broad coverage, targeted investigation, and explicit resolution of meaningful uncertainty.
---

Continue as the same single agent. Review the provisional profile and question index. The index represents the understanding to develop; it is not a requirement to conduct a separate investigation for every question.

Early in research, establish a broad picture across product scope, customer problems, use cases, people, and reasons to choose the product. Select sources that address weak areas of understanding. Do not let the availability of detailed technical documentation determine the whole research agenda.

Select important open questions that materially improve understanding or unlock other answers. Save the active question with update_questions before investigating. Reassess priorities as evidence arrives. Choose depth according to whether the answer changes the product explanation, customer fit, adoption requirements, or later research direction.

The active question guides source selection; it does not restrict learning. Apply each source to every question it informs, including questions that have never been active. Also consider whether it reveals an important gap not represented in the index.

For each question being updated, assess:

- What is now established, and on what evidence?
- What meaningful part remains unexplained?
- Should the next step be further research, refinement or splitting, interview deferral, or closure?

Keep these judgments in the existing answer, priority_reason, and closure_reason fields as appropriate. Do not create a separate tracking system.

A source being useful is not sufficient to mark a question answered. Close a question when the evidence and any clearly labeled inference address its substance well enough for the profile’s purpose.

When a question is partly answered, preserve the answer and keep the remaining uncertainty visible. Refine the question to the unresolved part, or create a focused follow-up when it warrants separate investigation. If closing the original question, explicitly identify any follow-up carrying its remaining material uncertainty.

Do not close a compound question merely because one component is well documented. A customer example can establish one use case without establishing the broader ideal customer, purchase trigger, or buying group. An operational workflow can establish likely users without establishing who approves the purchase.

Not every unknown needs further investigation. Leave minor details as scoped caveats when they would not materially change the understanding. Preserve a question when the uncertainty could change what the product enables, who needs it, its boundaries, adoption requirements, or reasons to choose it. Do not dismiss such uncertainty merely by calling it deployment-specific or deal-specific.

Reorder, add, refine, merge, reopen, or switch questions when appropriate. Keep every existing ID in each full update. For a merge, retain the original question with status merged, merged_into set to a distinct non-merged question ID, and closure_reason explaining the merge. Preserve its evidence and carry relevant findings into the surviving question. Resolve the surviving question before finishing. Merging must not erase distinct unanswered parts.

Choose sources according to the uncertainty. Search by customer problems, use cases, capabilities, and relevant terminology as well as the product name. Consider promising links already present in saved evidence. When sources describe different product generations, deployment models, or scopes, investigate the difference if it affects the explanation.

Use query_site_index: terms match any term across URL/title/description, ranked by matched terms then URL; required_phrase and url_contains can narrow results. Empty search browses. Inspect result relevance and pagination. A noisy query or an empty result page does not establish that useful leads are exhausted.

Read selected HTML pages. Company pages must be indexed or linked from saved evidence; external sources must be linked from company evidence. Do not use arbitrary URLs, external search, PDF/media processing, or additional agents. Discovery results are leads, not evidence.

Extract what sources explain about customer situations, existing difficulties, product contributions, practical use, and outcomes. Separately assess the strength of evidence for effectiveness. Vendor-published cases can inform understanding without independently validating their results. Keep individual examples within their stated scope.

Cite substantive claims with [S1] short source IDs. Distinguish documented behavior, vendor claims, customer-specific accounts, and inference. Answers may be documented, inferred, or mixed. Clearly labeled inference grounded in cited evidence with a brief explanation can answer a question. Important unsupported assumptions remain questions.

Revise the profile as understanding changes. Connect customer situations, problems, capabilities, outcomes, and conditions where relevant. Update dependent conclusions when a finding changes them. Use flexible structure suited to the product. Reuse existing context instead of fetching the same evidence again.

Save the full ordered question index with update_questions, preserving prior assessments. Assess only relevant successfully read sources: useful means evidence contributed information for that question. Record useful evidence against each affected question and reset its unproductive-page streak. Do not increment other questions’ streaks merely because a page did not address them. Code counts distinct unproductive pages and resets on useful evidence. Failed/blocked reads and cached rereads do not advance progress.

Three consecutive distinct relevant unproductive pages trigger reassessment and permit needs_interview; they do not by themselves establish that useful public research is exhausted. Consider whether the search approach fits the uncertainty and whether promising accessible leads remain. Exhausted plausible public leads permit earlier deferral with an explanation in closure_reason. Do not read pages merely to reach a count.

Before deferring, preserve established findings and narrow the question to the meaningful uncertainty that remains. Explain what was checked and how an interview could help. Say “unresolved through public research,” never that no public information exists. Distinguish information not found from material that could not be accessed or processed. An interview can clarify claims and identify evidence; it does not automatically provide independent validation. Later evidence may reopen or answer deferred questions.

Before finishing, review the profile independently of question statuses. For each foundational area, verify that it contains either a useful supported explanation or an explicit unresolved question where the gap matters. Check for major customer situations, product relationships, buying considerations, or other consequential gaps omitted from the original index or hidden inside answered questions.

Assess whether another researcher can explain what the product enables, who needs it, when it matters, how it changes their approach, and why they might choose it—while recognizing remaining uncertainty. An empty question queue alone does not establish sufficient understanding.

Finish with finish_research containing the final profile, full question_index, and reason. There must be no open questions. Interview questions are the needs_interview subset of this index, not a separate store. Budget or context exhaustion leaves the run incomplete; it never justifies automatically deferring questions or finishing. All exchanges are retained without compaction or silent truncation.