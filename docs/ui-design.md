# After Hours — dashboard redesign

October 24, 2026. The local application workspace now uses a dark graphite foundation, lime actions and violet accents. Space Grotesk gives headings and numbers a distinct voice; DM Sans keeps dense forms and records readable. Both fonts are served locally with their SIL Open Font License files. Pages request no external font service.

The visual centerpiece is an original CSS orbital diagram. It is decorative and hidden from assistive technology; it never implies live processing or fabricated progress. Motion is limited to view entry, hover and deliberate navigation, and respects reduced-motion preferences. Viewport restoration remains immediate so styling cannot animate a background refresh into a scroll jump.

## Layout and behavior

- A persistent desktop sidebar contains all five workspace views, an explicit new-pipeline action, connection diagnostics and the applicant identity. Short laptop screens omit the decorative callout so navigation and identity remain available. Phones use a compact horizontal navigation strip and a separate sticky context bar.
- The overview separates recorded company runs, running/queued agents, verified submissions in saved runs and job checkpoints. These are derived from existing host data. Service connectivity and browser-practice verification are separate signals.
- One quota banner explains an account-wide pause. Raw provider errors are not repeated across completed and stopped cards. The existing manual confirmation and resume gates are unchanged.
- Launch configuration and execution/capacity controls expand when needed. Each company card includes identity, stage activity, accurate metrics, accessible progress bars and existing actions. Long discovery reports, blockers and recovery details stay available behind disclosures.
- The applications view searches all saved records and displays twenty at a time with explicit result/page counts. Changing search resets pagination. Application details remain scoped to that view; navigating away and back preserves an open record, while explicitly closing it keeps it closed.
- Technology cards retain every Yes/No answer and complete experience note. Search matches names and context without turning related technologies into equivalent skills.
- The inbox keeps questions, contextual evidence, review material and scoped decisions. Existing keyed reconciliation and duplicate-action locks remain. Inputs have visible focus rings and accessible labels; notifications can be dismissed.
- The fictional local connection-test page uses the same dark palette and typography, with a direct return to the workspace. Its test-only behavior and required fields are unchanged.
- View URLs, browser back/forward, skip navigation, keyboard focus, native dark form controls and narrow-screen table scrolling remain supported. Core workflow, source facts and action approvals are unchanged.

## Verification

- The full isolated suite passed **220 tests**; one upstream Starlette/httpx deprecation warning remains. Source syntax, local asset delivery, static-file scope and existing editing/lifecycle protections passed. Evidence: `data/ui-redesign/20261024-tests.txt`.
- The actual browser checked all five views at **1440×1000, 1024×768, 768×1024, 390×844 and 320×740**. All twenty-five combinations fit the page width and retained exactly one active navigation item. Wide tables scroll inside their own container. Evidence: `20261024-responsive.json`.
- A separate 2560-pixel desktop check verified equal 406-pixel gutters around the content beside the sidebar, with no page overflow. The final focused UI, asset and browser-practice checks passed **23 tests** after the cosmetic/accessibility polish.
- A real isolated inbox fixture retained its text, focus and selection range **6–10** while the displayed refresh time advanced by **25 seconds**. No answer was sent. Evidence: `20261024-draft-polling.json` and `20261024-fictional-inbox.jpg`.
- Actual records pagination reached page 2, and searching C++ On-Vehicle Data reset it to page 1 with one matching saved record. The record detail opened and hid when navigating away. Technology search returned both the Spark and Flink notes with their distinct Yes and No answers intact.
- Final desktop/mobile screenshots, launch/history checks and the delivery audit are saved in `data/ui-redesign/`.
- A redundant repeat of the bulk browser matrix hit an embedded-preview debugger timeout. The completed twenty-five-case matrix was retained; the stalled owned preview was replaced through the supported browser API. The fresh preview loaded the final dark styles/fonts, showed no console errors and passed the final phone-header/scroll checks. This was a development preview recovery; application workers were not involved.
- The read-only preservation audit matches every original value across thirty-three tables; SQLite quick check is `ok`, and no application worker is active. No pipeline was resumed, no employer was contacted, and the synthetic fixture was stopped.

The theme lives in `app/static/theme.css`, layered over the existing component foundation. Workflow rendering remains in `app.js`; static IDs and protected routes are preserved. Font and SVG files are individually allowlisted rather than exposing the static directory. A pre-redesign copy of the three original frontend files is retained under `data/ui-redesign/20261024-before/`.
