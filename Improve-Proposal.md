# Proposal: Reconstructing the AI Marginalia System

## Overview

The current pipeline is technically sound but conceptually misaligned with the intended reading experience.

At present, the system behaves like an intelligent textbook annotator: it summarizes, explains, defines, and occasionally connects ideas. While this improves comprehension, it rarely recreates the experience of reading a traditionally annotated book—such as Chinese 古籍评点、脂砚斋批《红楼梦》、金圣叹评《水浒传》, or even the handwritten marginalia left by generations of careful readers.

The reconstruction should therefore begin from a change in philosophy rather than from prompt engineering alone.

The objective is no longer:

> Help the reader understand what this passage says.

Instead, it becomes:

> Accompany the reader during the act of reading by occasionally offering observations that reward closer attention.

This shift affects every layer of the system.

---

# Part I — Redefining the Goal

The current prompts implicitly optimize for explanation.

They repeatedly encourage the model to:

* summarize
* define
* introduce
* connect
* gloss

These are all educational behaviors.

Traditional marginalia serves a different function.

It observes.

It comments.

It anticipates.

It questions.

It notices patterns.

It occasionally admires.

It occasionally warns.

It almost never attempts to explain every passage.

Therefore the guiding principle should become:

> An annotation should provide one rewarding insight that is unlikely to arise merely from understanding the literal text.

Understanding is the baseline.

Insight is the product.

---

# Part II — Chapter Memo Redesign

The current Chapter Memo functions primarily as a summary.

However, downstream annotation generation does not require another summary.

It requires memory.

Instead of describing the chapter, the memo should preserve everything a future annotator would otherwise forget after finishing the chapter.

The chapter memo should therefore evolve into a contextual memory rather than an abstract.

Recommended information includes:

• the narrative role of this chapter inside the whole book

• major semantic movements instead of topical outline

• recurring motifs and symbolic imagery

• concepts requiring contextual interpretation

• foreshadowing and delayed payoffs

• interpretive warnings

• emotional or rhetorical turning points

The memo should answer questions such as:

"What should I remember while annotating later passages?"

instead of

"What happened in this chapter?"

This distinction is fundamental.

---

# Part III — Annotation Prompt Redesign

The annotation prompt currently behaves like an explanatory assistant.

Instead, it should behave like a thoughtful reader writing in the margins.

The system prompt should explicitly redefine the role:

> Imagine another attentive reader will encounter this passage many years later.
>
> Leave only the observations that genuinely enrich the reading experience.
>
> Do not attempt to explain every paragraph.

Sparse annotation should become an explicit design objective.

A passage should remain unannotated unless at least one of the following conditions is true:

* it changes the reader's understanding

* it introduces an enduring motif

* it quietly foreshadows later developments

* it reveals hidden motivation

* it contains significant irony

* it compresses an important idea

* it benefits from historical or cultural context

* its literary craftsmanship deserves attention

The model should understand that silence is often preferable to redundancy.

---

# Part IV — Replace Explanation with Interpretation

Many current instructions encourage explanation.

For example:

"gloss a term"

"connect to the chapter thesis"

"introduce the section"

These produce textbook annotations.

Instead, annotations should prefer interpretation whenever possible.

Examples:

Instead of:

"This image symbolizes loneliness."

Prefer:

"The image isolates the protagonist long before the plot admits that isolation."

Instead of:

"This paragraph develops the chapter's thesis."

Prefer:

"Only here does the author's real concern finally become visible."

Instead of explaining meaning,

the annotation should reveal significance.

---

# Part V — Use Chapter Memo as Hidden Context

The annotation prompt currently instructs the model to explicitly connect every summary to the chapter memo.

This often produces repetitive academic language.

The memo should instead function as invisible context.

The reader should never feel that the annotation is quoting a hidden summary.

Instead, the annotation should naturally reflect that broader understanding without mentioning it directly.

The chapter memo becomes memory, not citation.

---

# Part VI — Encourage Personality

Traditional marginalia has a voice.

Sometimes admiring.

Sometimes skeptical.

Sometimes amused.

Sometimes quietly emotional.

The system should therefore allow restrained interpretive judgment.

Examples:

"This is the first moment his sincerity becomes unmistakable."

"The author deliberately withholds the conclusion."

"The irony becomes visible only in retrospect."

"The sentence appears casual but quietly determines everything that follows."

Such observations create companionship instead of documentation.

---

# Part VII — Reduce Annotation Density

Current language unintentionally encourages coverage.

Readers rarely enjoy annotations attached to every few paragraphs.

Instead, annotations should become occasional discoveries.

A useful guiding principle:

Excellent marginalia leaves many passages untouched.

Readers remember the comments precisely because they are rare.

---

# Part VIII — Future Improvements

Future iterations may include additional contextual information:

• chapter progress percentage

• previous annotation summaries

• recurring motifs collected across chapters

• character state memory

• thematic evolution throughout the book

These additions would significantly reduce repetitive introductions and improve long-range coherence.

---

# Part IX — CSS Review

The current stylesheet is clean, readable, and technically robust.

However, it still resembles a modern documentation callout rather than marginalia.

Its visual language emphasizes information blocks.

Traditional marginal notes should instead appear as quiet interruptions to reading.

Several adjustments are recommended.

---

## 1. Remove the full border

Current design:

left border + top border + bottom border + right border

This creates a rectangular box.

Boxes immediately suggest documentation, manuals, and software UI.

Traditional annotations should not feel boxed.

Recommendation:

Keep only the left rule.

Remove the remaining borders entirely.

The annotation should visually resemble a handwritten note beside a paragraph rather than a contained panel.

---

## 2. Reduce padding

Current:

padding: 0.6em 0.8em;

This makes each annotation occupy considerable vertical space.

Recommendation:

Reduce to approximately:

padding-left: 0.7em;

padding-top: 0.2em;

padding-bottom: 0.2em;

The annotation should breathe horizontally, not vertically.

---

## 3. Reduce vertical margins

Current:

margin-top: 1em

margin-bottom: 1.2em

This separates annotations too aggressively from the text.

Recommendation:

Around

0.4em

to

0.6em

above and below.

The annotation should feel attached to nearby paragraphs.

---

## 4. Use slightly smaller typography

Current:

0.95em

Recommendation:

Around

0.88–0.90em

Annotations should remain readable while clearly belonging to a secondary visual hierarchy.

---

## 5. Increase line compactness

Current:

line-height: 1.6

Recommendation:

Approximately

1.45

Marginalia should read more like notes than body text.

---

## 6. Remove italics from notes

Current NOTE style:

font-style: italic;

Long italic passages become noticeably harder to read on e-ink displays.

Instead, distinguish note types using border style and spacing only.

Typography should remain upright.

---

## 7. Make note types visually quieter

Current design relies on:

dashed

double

thicker borders

These differences are visually stronger than necessary.

Instead, distinguish kinds through subtle variations:

INTRO

thin dashed line

SUMMARY

slightly thicker solid line

NOTE

thin solid line with slightly increased indentation

The distinction should remain visible without drawing attention away from the book itself.

---

## 8. Narrow the annotation block

Current notes occupy nearly the same width as body paragraphs.

Instead, increase the left indentation slightly.

Readers should immediately perceive:

"This is not part of the original text."

without requiring heavy decoration.

---

## 9. Favor whitespace over decoration

Modern UI often separates components using borders.

Printed books traditionally separate commentary using whitespace.

Whenever possible, prefer spacing instead of additional visual elements.

The annotation should almost disappear until the reader decides to read it.

---

# Conclusion

The proposed reconstruction does not seek to make the system produce more annotations.

It seeks to make every annotation feel more intentional.

The reader should gradually experience the presence of an intelligent companion rather than an automated explainer.

A successful annotation is not one that explains the paragraph.

It is one that makes the reader pause briefly, look back at the text, and think:

"I might not have noticed that on my own."
