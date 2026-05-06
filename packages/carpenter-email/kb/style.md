# carpenter-email — outbound writing style

This article tells the **trusted chat agent** how the user prefers
their outbound email to be composed.  It is read by the agent when
preparing the `body` argument for `pkg_email_send_email`; it is
NEVER read by the REVIEWER (the REVIEWER has no KB access by
design).

This article is the one the user is most likely to iterate on after
launch.  Treat it as living guidance.

## Tone

* Conversational and concise.  Not formal unless the recipient
  context demands it (e.g. legal correspondence, an institution the
  user has not previously emailed informally).
* Default to first-person plural ("we") when speaking on behalf of
  a project; first-person singular ("I") when speaking personally.
* Never sycophantic.  Avoid openings like "I hope this email finds
  you well!" unless the user explicitly requested that tone.

## Length

* Short by default.  Aim for under 150 words for routine messages.
* For longer, explanatory emails (proposals, status updates), use
  short paragraphs with line breaks between them.  Don't pile up
  more than ~3 sentences per paragraph.

## Greetings and closings

* Greeting: lowercase "hi <name>," is the default.  "Hello <name>"
  for a more reserved tone.  No greeting at all is fine for very
  short replies in an existing thread.
* Closing: "Cheers," or "Best," or "Thanks," depending on context.
  Sign as "Ben" unless writing on behalf of a project, in which
  case sign with the project name.

## Structure

* Lead with the ask or the answer, not the context.  The user values
  recipients being able to skim the first sentence and know what the
  email is about.
* If you need to provide background, do it AFTER the ask, not
  before.

## Voice patterns the user prefers

* "Just want to confirm..." for verification requests.
* "Quick question:" for a short ask.
* "FYI" or "heads up" for unilateral notifications.
* Avoid "I wanted to reach out" — too padded.
* Avoid "circle back" — clichéd.

## Things to avoid

* Emojis (unless the user explicitly includes one in their request).
* Marketing-speak ("excited to share", "leveraging synergies").
* Excessive disclaimers ("Sorry to bother you, but...").
* Long honorifics ("Dear Mr. <surname>") unless the recipient
  context requires formality.

## Default signature

Unless the user specifies otherwise:

```
Cheers,
Ben
```

## When the user asks you to draft a reply

* Quote sparingly.  Do NOT paste the original message back at the
  recipient.
* If you need to reference a specific line from the original, quote
  only that line, indented.

## When to ask the user before sending

* Any send to a recipient the user has not previously sent to from
  this assistant flow.
* Any send with `body` longer than 200 words — confirm the draft.
* Any time the body would include numbers / amounts / dates the
  user did not explicitly provide.
