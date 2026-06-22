# Project Context: memores-admin

## Overview
memores-admin (v1.0.1) — Admin dashboard for a mental health/personality assessment platform (Sparks/Memores). Built with Vite 7 + React 18 + TypeScript 5.9, deployed to AWS S3/CloudFront.

## Tech Stack & Key Versions
| Layer | Choice |
|-------|--------|
| Build | Vite 7 + esbuild minifier |
| UI | React 18.3.1, Ant Design 5.29.3 |
| Styling | TailwindCSS v4 (primary) + SASS (overrides) |
| Routing | React Router v6 (`createBrowserRouter`) |
| Server State | TanStack React Query 5 |
| API Client | Native `fetch` wrapper (no Axios) |
| Auth | Token in localStorage + AES-CBC password encryption |
| i18n | i18next + HTTP backend (JSON files) |
| Charts | Recharts |
| Lint/Format | ESLint v9 (flat config) + Prettier |
| Testing | Jest setup only (no actual tests) |

## Architecture
```
src/
  api/          — fetch wrappers + endpoint modules (auth, user, content, etc.)
  components/   — shared UI (Page layout, SearchTable, form selects, text, icons)
  config/       — Ant Design theme (purple primary #40203c)
  constants/    — env, colors, course enums, Zod schemas, auth guards
  helpers/      — date/string/json/css/request utilities
  hooks/        — React Query domain hooks (20+ files) + auth/debounce/router
  pages/        — all route components (dashboard, users, benefactors, administration, content management)
  types/        — TypeScript type definitions (18 files)
  language/     — i18n config
```

- State management: No Redux/Zustand. React Query handles all server state; React Router loaders handle auth; Context for message API.
- Auth flow: `protectedLoader` checks localStorage token → calls `getUser()` → renders or redirects to `/login`. Login encrypts password with AES-CBC before POST.
- API layer: Custom fetch wrappers in `api/request.ts` with auto token injection and 401 redirect.

## Routes (18+ pages)
| Area | Routes |
|------|--------|
| Public | `/login` |
| Dashboard | `/` |
| Users | `/users`, `/users/:userId/:tabId?` (6 tabs) |
| Benefactors | `/benefactors`, `/benefactors/:benefactorId/:tabId?` |
| Administration | `/administration/:tabId?` (5 tabs), prompts CRUD, analysis outputs |
| Content Mgmt | `/content-management/:tabId?` (2 tabs), courses/questions/responses CRUD + upload |
| Other | `/alerts`, `/reports`, `/configuration` |

## Known Patterns Worth Reviewing
- No test coverage — setup file exists, zero test files
- `any` types — `@typescript-eslint/no-explicit-any` turned off; significant `any` usage
- `no-console` off — `console.log`/`console.debug` scattered through codebase
- Colors defined in 3 places — SCSS variables, Tailwind `@theme`, and TypeScript constants (drift risk)
- README is stale — says "Create React App" but uses Vite
- Polling — analysis outputs poll at 3-4s intervals; no WebSocket fallback
- Token storage — localStorage (`adminToken`), not httpOnly cookies; vulnerable to XSS
- Password encryption — AES-CBC client-side before POST; key+IV in env vars (security consideration)
- i18n debug enabled — `debug: true` in i18n config (will log to console in production)

## CI/CD
- GitHub Actions → push to main/dev triggers build + S3 sync + CloudFront invalidation
- Secrets fetched from AWS Secrets Manager at deploy time
- Node 25 in CI

## Recommended Review Focus Areas
1. **Auth security** — localStorage token, AES key/IV exposure, no refresh token rotation
2. **Error handling** — `RequestError` class exists, but check how errors surface to users
3. **Type safety** — `any` usage audit, especially in form helpers and table configs
4. **Color system** — sync between SCSS/Tailwind/TS; consider single source of truth
5. **Testing** — complete absence of tests for critical flows (auth, content CRUD)
6. **Build config** — `cross-env` for `VITE_ENV`, `VITE_URL_PATH` logic in `env.ts`
7. **i18n coverage** — check for hardcoded strings missing translation keys
8. **Bundle size** — Ant Design tree-shaking, unused dependencies
9. **Accessibility** — Ant Design provides baseline, but custom components lack a11y audit
10. **Data validation** — Zod for uploads, Ant Form rules for forms (inconsistent approaches)

## Key Files to Start With
| File | Why |
|------|-----|
| `src/api/request.ts` | Core fetch wrapper, token injection, error handling |
| `src/Router.tsx` | Route config, loaders, auth guards |
| `src/constants/env.ts` | Environment variable resolution |
| `src/config/theme.config.ts` | Ant Design theme tokens |
| `src/constants/auth.ts` | Auth guards and route constants |
| `eslint.config.js` | Lint rules and TS config |
| `vite.config.ts` | Build configuration and proxy |
| `.github/workflows/deploy.yml` | CI/CD pipeline |
| `src/hooks/api/*.tsx` | All React Query hooks (domain patterns) |
