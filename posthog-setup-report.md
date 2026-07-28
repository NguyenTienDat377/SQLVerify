<wizard-report>
# PostHog post-wizard report

The wizard has completed a deep integration of PostHog analytics into Skolem. The existing `core/analytics.py` module (which already captured `verification_run` events) was extended with 10 new capture functions and a user-identification helper. Events are now fired across every major business flow: authentication, billing, subscription lifecycle, project and API-key management, quota enforcement, and on-demand AI explanation requests. All events use the Supabase user UUID as `distinct_id`; PII (email) is sent only via `posthog_client.set()` (person properties), never in `capture()` event properties.

| Event | Description | File |
|---|---|---|
| `verification_run` | A SQL equivalence check was submitted, with status, surface, duration, and bound. | `api/verify.py` (pre-existing) |
| `user_logged_in` | A session was established via GitHub OAuth or magic link. | `api/auth.py` |
| `user_logged_out` | A user explicitly logged out. | `api/auth.py` |
| `checkout_initiated` | A user was redirected to the Lemon Squeezy checkout page for a plan. | `api/billing.py` |
| `subscription_updated` | A Lemon Squeezy subscription lifecycle event was received (created, cancelled, expired, etc.). | `api/webhooks.py` |
| `project_created` | A user successfully created a new project. | `api/projects.py` |
| `project_deleted` | A user deleted one of their projects. | `api/projects.py` |
| `api_key_created` | A user created a new API key for CI/CD access. | `api/keys.py` |
| `api_key_revoked` | A user revoked an existing API key. | `api/keys.py` |
| `quota_exceeded` | A free-tier user hit their monthly verification run limit. | `api/verify.py` |
| `explanation_requested` | A user requested an AI-generated explanation for a divergent run. | `api/verify.py` |

## Next steps

We've built some insights and a dashboard for you to keep an eye on user behavior, based on the events we just instrumented:

- **Dashboard:** [Analytics basics (wizard)](https://us.posthog.com/project/515087/dashboard/1857966)
- [Verification runs by status (wizard)](https://us.posthog.com/project/515087/insights/jfy0BQwt) — bar chart of verification outcomes by day
- [Verification runs by surface (wizard)](https://us.posthog.com/project/515087/insights/kup2bXmb) — web vs CI vs MCP usage over time
- [Login to checkout funnel (wizard)](https://us.posthog.com/project/515087/insights/eqSehVLb) — conversion from login → verification run → checkout
- [Quota exceeded events (wizard)](https://us.posthog.com/project/515087/insights/BY1l9Dir) — free-tier upgrade pressure signal
- [Checkout initiated by plan (wizard)](https://us.posthog.com/project/515087/insights/wJHE9c9Q) — individual vs team plan revenue signal

## Verify before merging

- [ ] Run a full production build (the wizard only verified the files it touched) and fix any lint or type errors introduced by the generated code.
- [ ] Run the test suite — call sites that were rewritten or instrumented may need updated mocks or fixtures.
- [ ] Add `POSTHOG_API_KEY` and `POSTHOG_HOST` to your Render environment variable panel (and any staging/preview environments). The `.env.example` already documents these keys.
- [ ] Confirm the returning-visitor path also calls `identify` — `set_session` identifies on every new token exchange (covering OAuth and magic-link), but a user who already has a valid session cookie will not re-identify on page load. This is expected for server-side-only tracking; note it if you add client-side PostHog later.

### Agent skill

We've left an agent skill folder in your project. You can use this context for further agent development when using Claude Code. This will help ensure the model provides the most up-to-date approaches for integrating PostHog.

</wizard-report>
