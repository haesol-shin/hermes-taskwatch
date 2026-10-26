# Principles

Seven rules shape what taskwatch does and does not do. Each has one line and one first-party source. The words after "which" in each item are what the source says, and the rule before it is how taskwatch applies it. The sources are linked so the wording can be checked at the link.

1. **The next step lives in a card or a file, never only in the chat.** Source: OpenAI, [Harness engineering](https://openai.com/index/harness-engineering/), which says knowledge that lives in chat threads is not accessible to the agent and so effectively does not exist.
2. **Checks are deterministic code, not instructions to a model.** Source: Claude Code docs, [Best practices](https://code.claude.com/docs/en/best-practices), which says hooks are deterministic and guarantee the action happens, unlike advisory instructions.
3. **A finished task is a claim until a check passes.** Source: Anthropic, [Effective harnesses for long-running agents](https://www.anthropic.com/research/effective-harnesses-for-long-running-agents), which reports agents declaring the job done too early and marking features complete without testing.
4. **A review loop has a round cap, and at the cap the approach is reconsidered.** Source: Claude Code team, [How the Claude Code team does code review](https://claude.dev/blog/solving-code-review-at-anthropic/), which caps review at three rounds and then asks the author to step back and reconsider the approach.
5. **The watcher notifies and does not run the work.** Source: [followthrough](https://github.com/BayramAnnakov/followthrough), whose README states that nothing runs an agent unattended. In taskwatch the scan writes warnings and tells the person; the gate does not execute or merge the work.
6. **A person is asked only when judgment is needed.** Source: OpenAI, [Harness engineering](https://openai.com/index/harness-engineering/), which lists escalating to a human only when judgment is required among what its agent can do.
7. **Every rule names the failure it blocks and when it can be removed.** Source: Anthropic, [Harness design for long-running apps](https://www.anthropic.com/research/harness-design-long-running-apps), which says every harness component encodes an assumption about what the model cannot do, and that these assumptions are worth stress testing because they can go stale.

## What these rules do not cover

For rule 6's approval trust boundary, see [Approval records](configuration.md#approval-records).

How these became checks: see [checks](checks.md).
