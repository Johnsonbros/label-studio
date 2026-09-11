# Topic-aware draft grading

Deployed model version: `cory-qwen35-9b-quality-topics-v3`. Taxonomy: 1.0.0. Existing rubric `jbp-phone-v1` is unchanged: accuracy/appropriate uncertainty 25, listening/clarification 20, professionalism 20, next step 25, efficiency 10. Do not compare these categories to a different proposed weighting without a rubric version change.

The grader requires exact transcript quotes for all five scores, each non-unclassified topic, and every suspected critical flag. It attaches matching segment timestamps. Quotes establish that words occurred, not that the model interpreted them correctly. Human review must check attribution and reasoning. External booking completion is always unverified without receipts. No audio delivery rating is generated from text.

Pilot transcripts now load from their private batch directory, retaining their timestamps. These base.en drafts and inferred speaker labels require audio-backed correction. A score is not training approval. A critical flag sets the draft disposition to critical_review regardless of numeric total.

Each accepted machine grade is stored in pipeline.sqlite `call_quality_grades`, keyed by task ID, model version and transcript hash, with five separate subscores, primary topic, confidence, full evidence JSON and machine_draft status. Secondary topics, workflow, urgency and outcome are in judgment_json.context. The table is created on the first persisted grade. Retries and daily limits remain in the existing judgments/meta tables. Cached grades are associated with each task on reuse. Old scored tasks are eligible for the new grader under the existing daily cap; this does not mean all calls have been regraded.

Example query after the first grade: SELECT primary_topic, count(*), avg(total) FROM call_quality_grades WHERE model_version='cory-qwen35-9b-quality-topics-v3' GROUP BY primary_topic;

The existing idle-call check and daily prediction limit remain. That check is not a preemptive GPU scheduler: a call arriving after a grading request begins can still contend with inference. GPU scheduler integration remains a separate coordination task. No extra bulk judge job was launched for this release.

Next: collect human reference scores on 25 varied calls, compare disagreements and speaker attribution, and expand to 50 before trusting judge rankings. Claude owns benchmark scenario coverage and supplies per-topic benchmark counts; automated gap tickets are not implemented by this change.
