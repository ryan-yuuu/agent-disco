---
name: Feature request
about: Suggest a capability, ergonomics change, or new tool for calfcord.
title: '[Feature] '
labels: enhancement
assignees: ''
---

<!--
The goal of this template is to surface the use case before the
implementation. A clear "what are you trying to do" + "who benefits"
often unlocks a simpler design than the one originally pitched.
-->

## Use case

What are you trying to do? Describe the concrete workflow that the
current calfcord doesn't support, not the feature you've already
designed in your head.

## Who benefits?

Operators? Agent authors? Tool authors? End users in Discord? Multiple?
A change that only helps one of those groups is fine — naming the
audience helps reviewers weigh the cost.

## Alternatives considered

Workarounds you've tried, related tools that solve adjacent problems,
ways to get most of the value without code changes (compose overrides,
env vars, a custom agent definition, etc.).

## Additive change or breaks existing behavior?

- [ ] Additive only — opt-in, no existing deployment behaves differently
- [ ] Breaks behavior — existing deployments need a migration or change

If it breaks behavior, sketch the migration path.

## Are you willing to implement?

- [ ] Yes, I'll open the PR
- [ ] Yes, with guidance on the design
- [ ] No, just filing the idea
