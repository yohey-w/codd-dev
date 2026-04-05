# CoDD Extract Prompt v3 (Framework-Agnostic)

## Purpose

Restore design-document artifacts from implementation code without guessing.
The output must be a structured YAML inventory that can be compared 1:1 with `baseline.yaml`.

## Non-Negotiable Rules

1. Source of truth is implementation only.
   Read real files. Never infer counts from naming conventions, screenshots, existing docs, or memory.
2. Every artifact gets one canonical owner layer.
   Cross-cutting concerns such as security, observability, and tenancy stay inside the layer that owns the artifact. Do not create a second category for them.
3. All numbers must be measured.
   No `~`, "about", or estimated counts. If a value cannot be measured, emit `null` and explain why.
4. Exclusions must be explicit and reproducible.
   Apply the exclusion globs before counting and print them in `meta.exclusions`.
5. Supporting evidence is not a second artifact.
   `layout.tsx`, `loading.tsx`, `template.tsx`, shared UI components, migrations, and docs can help interpretation, but do not emit them as primary items unless the project has no route/page entrypoints.
6. Every emitted item needs a stable unique key.
   Use `id`, `file`, and path-based identifiers so the result can be diffed mechanically.

## Default Exclusion Globs

```text
**/node_modules/**
**/.next/**
**/dist/**
**/build/**
**/coverage/**
**/.turbo/**
**/.cache/**
**/vendor/**
**/tmp/**
**/src/generated/**
**/generated/**
**/__snapshots__/**
**/*.d.ts
```

## Layer Mapping

Use artifact role first, folder name second.

| Layer | What belongs here | Next.js + Prisma example | Generic fallback |
|---|---|---|---|
| L1 Data Models | Schema entities and relations | `prisma/schema.prisma` models | `db/schema/**/*.sql`, ORM models, entity classes |
| L2 API Endpoints | Public request handlers | `app/api/**/route.ts` | `pages/api/**`, `src/routes/**`, `controllers/**` |
| L3 UI Pages | Route-owning screens and their supporting UI evidence | `app/**/page.tsx` | `src/pages/**`, route components, view entrypoints |
| L4 Business Logic | Domain/services/modules used by API or UI | `src/lib/**/*.ts` | `src/services/**`, `domain/**`, `usecases/**` |
| L5 Infra / Config | Deploy/runtime/config artifacts | `infra/*.bicep`, `*.config.*`, `.env.example` | Terraform, Docker, CI YAML, env templates |
| L6 Tests | Automated verification artifacts | `tests/**/*.test.ts` | `**/*.{test,spec}.*` |

## Canonical Ownership Rules

Apply these in order:

1. A Prisma/SQL/entity declaration is L1 even if it affects API or UI.
2. A request handler file is L2 even if it includes auth, metrics, or webhook verification.
3. A page or route-entry component is L3.
4. Shared components under `src/components/**` are supporting evidence for L3 unless the app has no file-routed pages.
5. Service, domain, helper, and orchestration modules are L4.
6. Infra, deploy, environment, and framework config files are L5.
7. Test files are L6.
8. Never create separate layers for security, observability, tenancy, or governance inside extract output. Keep those attributes on the owning item.

## Stack Discovery

### Step 0: Full Root Survey (MANDATORY — never skip)

Before any framework-specific scanning, enumerate ALL top-level directories and files.
This prevents blind spots for IaC, CI/CD, and non-framework artifacts.

```bash
pwd
ls -la
ls -d */ 2>/dev/null
find . -maxdepth 1 -type f | sort
find . -maxdepth 3 -type d | sort
```

**IaC & DevOps Pattern Scan** (run regardless of detected framework):

```bash
# IaC files anywhere in the repo
find . -type f \( \
  -name '*.bicep' -o -name '*.bicepparam' -o \
  -name '*.tf' -o -name '*.tfvars' -o \
  -name '*.tfstate' -o \
  -name 'Dockerfile*' -o -name 'docker-compose*' -o \
  -name '*.helmfile*' -o -name 'Chart.yaml' -o \
  -name 'serverless.yml' -o -name 'serverless.ts' -o \
  -name 'cdk.json' -o -name 'sam.yaml' -o -name 'SAM.yaml' -o \
  -name 'Procfile' -o -name 'app.yaml' -o -name 'fly.toml' -o \
  -name 'render.yaml' -o -name 'vercel.json' -o -name 'netlify.toml' \
\) 2>/dev/null | grep -v node_modules | sort

# CI/CD pipelines
find . -maxdepth 3 -type f \( \
  -path '*/.github/workflows/*' -o \
  -path '*/.gitlab-ci*' -o \
  -path '*/.circleci/*' -o \
  -name 'Jenkinsfile' -o \
  -name 'azure-pipelines.yml' -o \
  -name 'bitbucket-pipelines.yml' \
\) 2>/dev/null | sort
```

Any file found in this step belongs to L5 and MUST be included in the extract.

### Step 1: Framework Detection

Detect the framework and map candidate roots:

```bash
cat package.json 2>/dev/null | head -50
cat requirements.txt 2>/dev/null || cat Pipfile 2>/dev/null || cat pyproject.toml 2>/dev/null
cat Gemfile 2>/dev/null
cat go.mod 2>/dev/null
cat composer.json 2>/dev/null
cat pom.xml 2>/dev/null | head -30
cat build.gradle 2>/dev/null | head -30
```

Then map candidate roots:

- Data: `prisma/`, `db/`, `models/`, `database/migrations/`, `alembic/`, `entity/`
- API: `app/api/`, `pages/api/`, `src/routes/`, `routes/`, `app/Http/Controllers/`, `controllers/`
- UI: `app/`, `src/pages/`, `pages/`, `src/components/`, `resources/views/`, `templates/`
- Logic: `src/lib/`, `src/services/`, `src/domain/`, `app/Services/`, `app/UseCases/`
- Infra: `infra/`, `deploy/`, `k8s/`, `terraform/`, `cdk/`, root `*.config.*`, `.env*`, CI folders
- Tests: `tests/`, `test/`, `spec/`, `src/**/__tests__/`, `src/**/*.{test,spec}.*`

If a default path does not exist, fall back to the generic equivalent instead of failing.

## Extraction Procedure

### Step 1: Enumerate Primary Candidates

Run real file enumeration commands and store raw output.

#### L1 Data Models

Preferred:

```bash
rg '^model ' prisma/schema.prisma
```

Fallbacks:

```bash
find db -type f \( -name '*.sql' -o -name '*.prisma' \) | sort
find src/models -type f | sort
```

Emit one item per model/entity:

```yaml
- id: model:User
  name: User
  file: prisma/schema.prisma
  fields: 12
  relations: 5
```

#### L2 API Endpoints

Preferred:

```bash
find app/api -name 'route.ts' | sort
```

Fallbacks:

```bash
find pages/api -type f | sort
find src/routes -type f | sort
find src/controllers -type f | sort
```

Emit one item per handler file:

```yaml
- id: api:app/api/v1/payments/checkout/route.ts
  path: /api/v1/payments/checkout
  file: app/api/v1/payments/checkout/route.ts
  methods: [POST]
  auth_required: true
```

#### L3 UI Pages

Preferred:

```bash
find app -name 'page.tsx' | sort
```

Supporting evidence:

```bash
find app \( -name 'layout.tsx' -o -name 'loading.tsx' -o -name 'template.tsx' \) | sort
rg --files src/components
```

Emit one primary item per route-owning page:

```yaml
- id: ui:/learner/courses/[courseId]
  path: /learner/courses/[courseId]
  role: learner
  file: app/(dashboard)/learner/courses/[courseId]/page.tsx
  evidence:
    components:
      - src/components/lesson/LessonViewer.tsx
```

Important:

- Count every `page.tsx`, including root public pages and auth pages.
- Do not count `layout.tsx` as a page.
- Shared components are evidence, not separate L3 items, unless the project has no route pages.

#### L4 Business Logic

Preferred:

```bash
find src/lib -name '*.ts' | sort | grep -v '/generated/'
```

Fallbacks:

```bash
find src/services src/domain -name '*.ts' 2>/dev/null | sort
```

Emit one item per module:

```yaml
- id: logic:src/lib/stripe-billing.ts
  name: stripe-billing.ts
  file: src/lib/stripe-billing.ts
  exports:
    - createCheckoutSession
    - handleStripeWebhookEvent
  lines: 791
```

#### L5 Infra / Config

**CRITICAL**: Do not rely solely on framework-convention paths. Always include Step 0 results.

Preferred:

```bash
# IaC directories (check ALL of these, not just the ones your framework uses)
for dir in infra deploy terraform cdk k8s helm .aws .azure cloudformation; do
  [ -d "$dir" ] && find "$dir" -type f | sort
done

# Root-level config and deploy files
find . -maxdepth 2 -type f \( \
  -name '*.config.*' -o -name '.env.example' -o -name '.env.local' -o \
  -name 'Dockerfile*' -o -name 'docker-compose*' -o \
  -name '*.bicep' -o -name '*.bicepparam' -o \
  -name '*.tf' -o -name '*.tfvars' -o \
  -name 'serverless.*' -o -name 'cdk.json' -o \
  -name 'fly.toml' -o -name 'vercel.json' -o -name 'netlify.toml' -o \
  -name 'render.yaml' -o -name 'Procfile' -o -name 'app.yaml' \
\) 2>/dev/null | grep -v node_modules | sort

# CI/CD pipelines
find . -maxdepth 3 \( -path '*/.github/workflows/*' -o -path '*/.gitlab-ci*' -o -path '*/.circleci/*' -o -name 'Jenkinsfile' -o -name 'azure-pipelines.yml' \) 2>/dev/null | sort
```

Emit one item per deploy/runtime/config file:

```yaml
- id: infra:infra/main.bicep
  name: main.bicep
  file: infra/main.bicep
  type: azure_iac
```

#### L6 Tests

Preferred:

```bash
find tests -type f \( -name '*.test.ts' -o -name '*.spec.ts' -o -name '*.test.tsx' -o -name '*.spec.tsx' \) | sort
```

Fallbacks:

```bash
find src -type f \( -name '*.test.*' -o -name '*.spec.*' \) | sort
```

Emit one item per test file:

```yaml
- id: test:tests/unit/sprint_6/metrics.test.ts
  file: tests/unit/sprint_6/metrics.test.ts
  suites: 1
  tests: 7
```

### Step 2: Measure, Do Not Summarize

For each item, measure from file content:

- L1: field count, relation count
- L2: HTTP methods, path params, auth requirement if explicit
- L3: route path, role, server action presence if explicit
- L4: exported symbols, line count
- L5: file type, key settings or env var names
- L6: suite count, test count

If a metric is not recoverable from code, emit `null`.

### Step 3: MECE Audit

Run these checks before finalizing:

1. `count(raw candidates) == count(emitted items)` for each layer.
2. No `file` appears in two layers.
3. No approximate wording in output.
4. `page.tsx` coverage includes root public routes and auth routes.
5. Security/metrics/webhook artifacts remain in their owner layer:
   - `csv-parser.ts`, `csv-export.ts`, `sanitizeHtml`, `timingSafeEqual` stay in L4
   - `/api/health`, `/api/metrics`, webhook routes stay in L2
6. Supporting files are not double-counted as primary artifacts.

Emit audit results:

```yaml
quality_checks:
  mece:
    duplicate_files: []
    missing_paths: []
    estimated_values: []
    count_mismatches: []
```

### Step 4: Output Schema

Return YAML compatible with `baseline.yaml` shape:

```yaml
meta:
  generated_at: "2026-04-04T00:00:00+09:00"
  repo: /path/to/repo
  stack:
    framework: nextjs_app_router
    orm: prisma
    language: typescript
  exclusions:
    - "**/node_modules/**"
  total_artifacts: 0

layers:
  L1_data_models:
    count: 0
    items: []
  L2_api_endpoints:
    count: 0
    items: []
  L3_ui_pages:
    count: 0
    items: []
  L4_business_logic:
    count: 0
    items: []
  L5_infra_config:
    count: 0
    items: []
  L6_tests:
    count: 0
    items: []

quality_checks:
  mece:
    duplicate_files: []
    missing_paths: []
    estimated_values: []
    count_mismatches: []
```

## Failure Guards Learned From cmd_313

Apply these checks explicitly:

1. Do not restrict UI extraction to `app/(dashboard)` only.
   Count `app/page.tsx` and auth routes too.
2. Validate subgroup labels against enumerated rows.
   If a section says "11 screens" but lists 14, the extract is invalid.
3. Do not mix source code and prose docs as equal evidence for layer counts.
   Infra counts come from infra/config files first; docs are commentary only.
4. Do not create a separate "security" bucket for logic modules.
   Security controls are attributes of L2/L4/L5 artifacts.
5. Do not omit tests from the layer inventory.
6. If `baseline.yaml` is missing, still emit the exact schema above so later diffing remains possible.

## Generic Adaptation Notes

This prompt must remain reusable beyond osato-lms:

- Replace directory paths with equivalent stack paths, but keep the six roles unchanged.
- Do not hardcode LMS-specific nouns, roles, or route names.
- Prefer artifact semantics over folder names.
- If the project uses a different framework, keep the same output schema and only swap discovery commands.

## Expected Result

The extract should be machine-diffable, MECE-audited, and exact enough for before/after comparison without manual cleanup.
