# General engineering

## Task guidance

- Record useful lasting decisions in existing instructions or relevant docs; do
  not create config or documents for trivial preferences.
- Prefer explicit names and clear control flow over dense expressions. Explain
  non-obvious constraints, not syntax.
- Separate business decisions from independently changing presentation,
  transport, and persistence. Pass dependencies explicitly; add interfaces or
  layers only for required boundaries or substitutions.
- Reuse suitable code, standard libraries, and platform APIs. Suggest or add
  tools, dependencies, abstractions, services, or processes only for current
  requirements or concrete risks with meaningful benefit. Avoid repeated
  unrelated suggestions; choose the simplest adequate solution.
- Model meaningful states and failures explicitly; distinguish absent, empty,
  invalid, and failed results where callers need it. Never silently turn
  failures into successful defaults or discard error context.
- Give resources an owner and cleanup across success, failure, and cancellation.
  Define ordering for shared mutable state and overlapping work.
- Optimize measured bottlenecks with representative workloads. Define cache
  lifetime, keys, and invalidation; avoid speculative complexity.
- Keep one authoritative explanation per topic; update affected setup, usage,
  contracts, and limitations. Keep README a concise entry point; put advanced
  details in existing docs first.
- Keep public docs relevant to project users. Exclude unrelated promotion,
  personal infrastructure, and private development context without user value.
  Preserve copyright, licensing, provenance, legitimate contributor and support
  info, and useful external links.
- Update affected website or documentation sources alongside repository changes:
  descriptions, examples, versions, compatibility. Check for published content;
  reuse site architecture, styling, generator, and validation. Add no unrelated
  site.
