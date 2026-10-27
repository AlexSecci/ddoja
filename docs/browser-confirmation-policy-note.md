# Browser confirmation policy checked on 26 October 2026

Source: the live **Computer/Browser Use Confirmation Policy** returned by `cua.getState()` in this conversation. This is a runtime instruction, separate from our dashboard's own workflow guards.

Its section **Confirmation Required at Action time** explicitly lists:

- “Solving/completing CAPTCHAs”
- “Accepts a legally binding agreement”

The policy defines action-time confirmation as required even after pre-approval. A model change, dashboard consent row, Chrome's full-CDP switch, or a different browser automation library does not waive that instruction. It also identifies materially reducing approval requirements as a protected action.

Both operations are technically executable by an agent after the required confirmation. This is an authorization constraint, not a claim that all automated applications are legally prohibited or that a person must physically operate each control.

The policy allows batching relevant currently presented confirmations. It excludes viewing non-binding notices from agreement acceptance. Existing applicable approvals should not be requested again unless the action, destination, data, terms or risk materially changes. Routine authorized résumé uploads and sign-in can be automated under their separate authorization rules.

A genuinely supported execution flow with different authorization semantics, or an official employer integration with its own valid authorization mechanism, would need to be evaluated under its applicable rules. This is not evidence that any such flow is currently available for these employers. We cannot replace the tool merely to evade its confirmation requirements or bypass a site's challenges.
