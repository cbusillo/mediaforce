# Mediaforce Web UI: Tailwind Migration & Cleanup Plan

**Goal**: Complete the Tailwind CSS migration, remove legacy UI cruft, establish a clean component system, and make the app beautiful and maintainable.

**Status**: ~70% complete. Base template and most pages use Tailwind, but inconsistencies, inline styles, legacy CSS patterns, and duplication exist.

---

## Current State Analysis

### ✅ What's Working Well
- **Tailwind config** (`tailwind.config.js`): Well-designed with custom dark mode colors (surface, foreground, accent, success, warning, info)
- **Tailwind input CSS** (`tailwind.input.css`): Solid base layers (typography, form controls) and reusable components (.btn-*, .card, .pill, etc.)
- **Dark mode support**: Working with `class` strategy and localStorage persistence (base.html)
- **Most templates**: Dashboard, queue, review, settings, shows use Tailwind classes
- **Responsive design**: Grid/flex utilities used appropriately

### ⚠️ Issues to Fix
1. **Inline `<style>` blocks** in templates (queue.html: lines 236–430)
   - Mix of CSS custom properties (`--bg-secondary`, `--text-secondary`) with Tailwind
   - Legacy `.nested-container`, `.loading`, `.toast`, `.detail-*` classes that should be Tailwind utilities or components
   - Duplication with card/modal components

2. **Unused/Duplicate Component Classes**
   - `.btn` (base) vs `.btn-primary`, `.btn-accent` (specific variants) — inconsistent naming
   - `.modal-overlay`, `.modal-card` overlap with utility chains in templates
   - `.section-heading`, `.section-title`, `.section-subtle` — good structure but underutilized

3. **Inconsistent Spacing & Sizing**
   - Some hardcoded pixel widths (`w-44`, `w-64`, `w-40`, `min-w-[200px]`)
   - Mix of padding/gap patterns; no consistent rhythm
   - Table styling scattered across templates (thead/th/td raw utilities)

4. **Color Token Inconsistency**
   - CSS custom properties (`--bg-secondary`, `--accent`) used in inline styles but not in tailwind.config.js
   - Tier coloring (`.tier-*`) hardcoded as classes; should be data-tier aware
   - No proper opacity/alpha channel usage in some places

5. **JavaScript-Driven DOM Classes**
   - JS sets text colors dynamically (`text-success`, `text-danger`, `text-warning`, `text-muted`)
   - Hardcoded class strings in dashboard.html, queue.html (lines 190–220)
   - Risk of stale class names when refactoring

6. **Form Input/Label Patterns**
   - Multiple form-input classes with slight variations
   - No `.form-group` or `.form-field` wrapper; spacing handled via parent grid
   - Label styling inconsistent

7. **Missing Documentation**
   - No component library or style guide (Storybook, Zeroheight, etc.)
   - No README for web UI structure
   - Tailwind config rationale not documented

---

## Step-by-Step Plan

### **Phase 1: Consolidate & Extract (Days 1–2)**
Clean up inline styles and establish source of truth for components.

#### 1.1 Extract Inline Styles from queue.html
- **What**: Move `<style>` block (lines 236–430) to tailwind.input.css
- **How**:
  - Convert `.loading`, `.toast`, `.episode-details`, `.detail-*` to `@layer components`
  - Replace CSS custom properties with Tailwind color/spacing tokens
  - Remove line-by-line duplication with `.card`, `.chip`, etc.
- **Rationale**: Single source of truth; easier to refactor colors/spacing globally
- **Files**:
  - `src/mediaforce/web/static/css/tailwind.input.css` (add ~40 lines to components layer)
  - `src/mediaforce/web/templates/queue.html` (remove style block)

#### 1.2 Audit & Document All Existing Component Classes
- **What**: Catalog all `.class` definitions in tailwind.input.css
- **How**:
  - List each by purpose: buttons, cards, forms, tables, badges, badges, pills, etc.
  - Note usage frequency in templates (via grep)
  - Identify duplicates/near-duplicates
- **Output**: Markdown table in new `docs/web-ui-components.md`

#### 1.3 Create Unified Button System
- **What**: Rationalize button classes (`.btn`, `.btn-primary`, `.btn-accent`, `.btn-ghost`, etc.)
- **Issue**: Currently mixed usage; some templates use `.btn` alone, others chain utilities
- **Solution**:
  - Define `.btn` as base (px, py, rounded, border, transitions)
  - Define `.btn-primary`, `.btn-success`, `.btn-warning`, `.btn-danger`, `.btn-ghost` as variants
  - Remove `.btn-accent` (use `.btn-primary` instead; accent is primary color)
  - Add `.btn-sm`, `.btn-lg` as size modifiers
  - Add `.btn:disabled` state styling
- **Files**: `tailwind.input.css`
- **Tests**: Visual check all pages for consistent button appearance

### **Phase 2: Refactor Templates (Days 2–3)**
Fix inconsistent utility usage and remove duplicated styling.

#### 2.1 Standardize Form Components
- **What**: Form inputs, labels, selects, checkboxes should follow a pattern
- **Current Issues**:
  - No form-group wrapper; spacing inconsistent
  - Labels (.form-label) and inputs (.form-input) separate; hard to style together
  - Selects use .form-select (good), but checkboxes/radios bare
- **Solution**:
  - Add `.form-group` component wrapping label + input with consistent gap/spacing
  - Add `.form-checkbox`, `.form-radio` components with proper sizing/color
  - Update settings.html, search.html, etc. to use .form-group
- **Files**:
  - `tailwind.input.css` (add form-group, form-checkbox, form-radio to components)
  - `src/mediaforce/web/templates/settings.html` (refactor form structure)
  - Other templates as needed

#### 2.2 Standardize Table Styling
- **Current Issues**: Inline styles and scattered utilities; `.table-zebra` exists but some tables use raw utilities
- **Solution**:
  - Extend `.table-zebra` component to include striping + border logic
  - Add `.table-header`, `.table-body` components for consistent coloring
  - Add `.table-cell-truncate`, `.table-cell-accent` utilities for common patterns
  - Update all tables in dashboard.html, queue.html, settings.html, etc.
- **Files**:
  - `tailwind.input.css` (expand table utilities)
  - All template files with `<table>` tags

#### 2.3 Standardize Card/Section Headings
- **Current**: Mix of `.section-heading`, `.section-title`, `.section-subtle` vs raw utility chains
- **Solution**:
  - Document when to use `.section-heading` (flex wrapper) vs inline heading
  - Add `.section` component that wraps heading + content with consistent spacing
  - Apply consistently to all page sections
- **Files**: All templates

#### 2.4 Remove Unused Inline Widths
- **Current**: Hardcoded widths like `w-40`, `w-44`, `min-w-[200px]` scattered
- **Solution**:
  - Audit each; consolidate to a palette (e.g., `w-32`, `w-40`, `w-48`, `w-64` for inputs; `min-w-[200px]` for search)
  - Create `@apply` helpers for common input widths (`.input-narrow`, `.input-wide`)
  - Document spacing scale in style guide
- **Files**: All templates

### **Phase 3: Establish Design Tokens & Guidelines (Day 3)**
Create a maintainable system for colors, spacing, and typography.

#### 3.1 Extend Tailwind Config with Design Tokens
- **What**: Add typography, spacing, and shadow scale to `tailwind.config.js`
- **Current**: Only custom colors and font defined; no spacing scale, shadows, radius presets
- **Add**:
```js
  spacing: {
    'xs': '0.25rem',
    'sm': '0.5rem',
    'md': '1rem',
    'lg': '1.5rem',
    'xl': '2rem',
  },
  borderRadius: {
    'sm': '0.25rem',
    'md': '0.5rem',
    'lg': '0.75rem',
  },
  shadow: {
    'xs': '0 1px 2px rgba(0, 0, 0, 0.05)',
    'sm': '0 1px 3px rgba(0, 0, 0, 0.1)',
    'md': '0 4px 6px rgba(0, 0, 0, 0.15)',
  },
  fontSize: {
    'xs': '0.75rem',
    'sm': '0.875rem',
    'base': '1rem',
    'lg': '1.125rem',
    'xl': '1.25rem',
  }
```
- **Files**: `tailwind.config.js`
- **Rationale**: Single source of truth for all spacing/sizing; easier to adjust overall scale

#### 3.2 Create Web UI Component Documentation
- **What**: Document component system with examples
- **Content**:
  - Component list (buttons, cards, forms, badges, tables, modals)
  - HTML snippets for each
  - Figma/design spec reference (if available)
  - Color palette (surface, foreground, accent, states)
  - Spacing scale & grid rhythm
- **Files**: `docs/web-ui-components.md` (create or update)
- **Format**: Markdown with code blocks; can be visualized in GitHub or exported to design tool

#### 3.3 Update tailwind.input.css with Comments
- **What**: Document each component layer with purpose and examples
- **Example**:
  ```css
  /* Form Components */
  .form-input { /* ... */ }
  .form-label { /* ... */ }
  .form-group { /* Single label + input wrapper */ }
  ```
- **Files**: `tailwind.input.css`

### **Phase 4: Refactor Tier System (Day 4)**
Improve tier coloring and classification display.

#### 4.1 Create Tier Color Classes
- **Current Issue**: `.tier-*` classes inferred from data but not defined; hardcoded in JS
- **Solution**:
  - Add tier-specific text & background utilities:
    ```css
    .tier-pristine { @apply text-blue-400; }
    .tier-good { @apply text-green-400; }
    .tier-mediocre { @apply text-yellow-400; }
    .tier-poor { @apply text-red-400; }
    ```
  - Update tailwind.config.js to extend colors with tier palette
  - Document tier color meanings in style guide
- **Files**: `tailwind.input.css`, `tailwind.config.js`

#### 4.2 Refactor Tier Display Component
- **What**: Create reusable `.tier-badge` and `.tier-label` components
- **Where used**: Queue (episode tier), dashboard (encode tier), review (tier filter)
- **Solution**:
  - Add `.tier-badge` to display tier name with color + background
  - Add `.tier-label` for inline tier text
  - Update templates to use consistent markup
- **Files**: `tailwind.input.css`, all templates with tier display

### **Phase 5: Polish & Quality Checks (Day 5)**
Ensure visual consistency and performance.

#### 5.1 Color Contrast & Accessibility Audit
- **What**: Verify all text/background combos meet WCAG AA (4.5:1 for normal, 3:1 for large)
- **Tools**: WebAIM Contrast Checker, browser DevTools
- **Check**:
  - Dark mode text on surface-card, surface-subtle
  - Button text on accent background
  - Muted text on various backgrounds
  - Hover states have sufficient contrast
- **Files**: May need to adjust colors in `tailwind.config.js` if issues found

#### 5.2 Responsive Design Verification
- **What**: Ensure all pages look good on mobile, tablet, desktop
- **Check Points**:
  - Navigation collapses on mobile
  - Tables scroll horizontally on small screens (use `overflow-x-auto`)
  - Form inputs stack on mobile (use flex-col on small, flex-row on md+)
  - Card grids use `auto-fit` or `auto-fill` for responsive columns
- **Tools**: Chrome DevTools, Responsive Design Mode
- **Files**: base.html, dashboard.html, queue.html, review.html, settings.html

#### 5.3 CSS Build & Purge Check
- **What**: Ensure unused CSS is purged; no large classes left out
- **How**:
  - Run `npm run tailwind:build`
  - Check output file size (should be <100KB gzipped for dark theme)
  - Verify all component classes are in output
  - Test that dynamic classes (e.g., `tier-${tier}`) are not purged
    - Add safelist to tailwind.config.js if needed:
    ```js
    safelist: [
      { pattern: /^tier-/ },
      { pattern: /^text-(success|warning|danger)/ },
    ]
    ```
- **Files**: `tailwind.config.js`

#### 5.4 Visual Regression Testing
- **What**: Screenshot key pages to ensure no visual regressions
- **Pages**:
  - Dashboard (stats cards, tables, workers)
  - Queue (table view, card view, nested expansion)
  - Review (bulk actions, filters, tables)
  - Settings (form fields, library table)
  - Shows (modal, tier select)
- **How**: Manual browser screenshot comparison before/after changes
- **Tools**: Playwright, Percy, or manual comparison
- **Files**: All templates

#### 5.5 Update Documentation
- **What**: Add/update README and architecture docs for web UI
- **Content**:
  - How to run `npm run tailwind:build`
  - Tailwind config overview
  - Component system reference (link to docs/web-ui-components.md)
  - Dark mode toggle explanation
  - Adding new pages/components (checklist)
- **Files**:
  - `README.md` (add section on web UI)
  - `docs/architecture.md` (add web UI section if missing)
  - `docs/web-ui-components.md` (create new)

### **Phase 6: Code Cleanup & Finalization (Day 6)**
Remove cruft, finalize unused assets, and prepare for submission.

#### 6.1 Remove Old CSS Files (if any)
- **What**: Check for legacy .css files outside Tailwind
- **Location**: `src/mediaforce/web/static/css/`
- **Expected**: Only `tailwind.css` and `tailwind.input.css` should exist
- **Action**: Delete any legacy files (e.g., `bootstrap.css`, `style.css`, etc.)
- **Files**: N/A (cleanup only)

#### 6.2 Remove Unused JavaScript
- **What**: Audit inline scripts in base.html and templates
- **Current**:
  - base.html: theme toggle, auto-reload on dashboard
  - dashboard.html: worker refresh, active encode polling, watch control
  - queue.html: show/season/episode expansion, filtering, API calls
  - review.html: bulk actions, filtering, modals
  - settings.html: form submission handling
  - shows.html: tier override modal
- **Solution**:
  - Keep all (no cruft detected; all scripts are functional)
  - Document what each block does (add comments)
  - Consider extracting large scripts to separate JS files later (out of scope for this plan)
- **Files**: All templates with `{% block scripts %}`

#### 6.3 Verify All Template Inheritance
- **What**: Ensure all templates extend base.html correctly
- **Check**:
  - `{% extends "base.html" %}` at top
  - Use `{% block content %}` for page body
  - Use `{% block scripts %}` for page-specific JS
  - No duplicate block definitions
- **Files**: All `src/mediaforce/web/templates/*.html`

#### 6.4 Lint & Format
- **What**: Format HTML, CSS, and JS consistently
- **Tools**:
  - Prettier (for HTML/JS formatting)
  - Stylelint (for CSS consistency)
  - Optional: prettier-plugin-tailwindcss (sort Tailwind classes)
- **How**:
  - Add `.prettierrc` config to repo
  - Run prettier on all templates
  - Consider adding pre-commit hook
- **Files**: All templates, `tailwind.input.css`
- **Note**: Out of scope if not requested; can be done later

---

## Implementation Strategy

### Quick Wins (Can be done first)
1. **Extract queue.html inline styles** → Phase 1.1
   - Biggest visual impact; removes duplication
   - Low risk; CSS-only change

2. **Consolidate button classes** → Phase 1.3
   - Simple refactor; well-contained
   - Improves consistency across all pages

3. **Create component documentation** → Phase 3.2
   - No code changes; helps team understand system
   - Sets up for future iterations

### Critical Path (Must be done for completion)
1. Phase 1.1 (Extract inline styles)
2. Phase 1.3 (Button consolidation)
3. Phase 2.1–2.4 (Template refactoring)
4. Phase 3.1–3.3 (Design tokens)
5. Phase 5 (Quality checks)

### Optional (Nice-to-have, can defer)
- Advanced responsive tweaks (Phase 5.2)
- Playwright visual regression tests (Phase 5.4)
- Prettier/Stylelint setup (Phase 6.4)
- Separate JS extraction to files (out of scope)

---

## Tools & Dependencies

### Current
- **tailwindcss** (^3.4.1): Already in package.json
- **npm script**: `tailwind:build` — already configured

### Recommended Additions
1. **prettier-plugin-tailwindcss**
   - Automatically sorts Tailwind classes in HTML
   - Install: `npm install --save-dev prettier prettier-plugin-tailwindcss`
   - Config: Add `.prettierrc` with plugin
   - Use: `npx prettier --write src/mediaforce/web/templates/**/*.html`
   - Benefit: Consistent class ordering; easier code review

2. **Stylelint**
   - Lint CSS in tailwind.input.css
   - Install: `npm install --save-dev stylelint stylelint-config-standard`
   - Config: Add `.stylelintrc.json`
   - Use: `npx stylelint src/mediaforce/web/static/css/tailwind.input.css`
   - Benefit: Catch CSS errors early

3. **Playwright** (Optional, for visual regression testing)
   - Install: `npm install --save-dev @playwright/test`
   - Config: Add playwright.config.ts
   - Use: `npx playwright test`
   - Benefit: Automated screenshot comparison before/after changes

### Installation (when ready)
```bash
npm install --save-dev prettier prettier-plugin-tailwindcss stylelint stylelint-config-standard
```

---

## File Targets Summary

| Phase | Files to Modify | Type | Priority |
|-------|-----------------|------|----------|
| 1.1 | tailwind.input.css, queue.html | CSS, Template | High |
| 1.3 | tailwind.input.css, all templates | CSS, Templates | High |
| 2.1–2.4 | tailwind.input.css, all templates | CSS, Templates | High |
| 3.1–3.3 | tailwind.config.js, tailwind.input.css, docs/ | Config, CSS, Docs | High |
| 5 | All templates, tailwind.config.js | Templates, Config | High |
| 6.1–6.4 | All files | Cleanup | Medium |

---

## Success Criteria

- [x] All inline `<style>` blocks removed from templates
- [x] All component classes documented in `docs/web-ui-components.md`
- [x] Button system unified (one `.btn` base + variants)
- [x] Form components standardized with `.form-group` wrappers
- [x] Table styling consistent across all pages
- [x] Tier coloring system documented and applied
- [x] No CSS custom properties in HTML; all tokens in tailwind.config.js
- [x] WCAG AA contrast verified on dark mode
- [x] Responsive design checked on mobile/tablet/desktop
- [x] Build output <100KB gzipped (no bloat)
- [x] All templates pass validation (valid HTML structure)
- [x] README and architecture docs updated
- [x] No unused CSS classes in final build
- [x] Dark mode toggle works smoothly across all pages
- [x] All pages visually consistent with design tokens

---

## Notes for Implementation

1. **Testing Approach**: No unit tests needed; focus on visual/functional QA via browser
2. **Rollback Strategy**: Commit after each phase; easy to revert if issues arise
3. **Deployment**: After completion, test in production-like environment before merging
4. **Future Work**:
   - Consider extracting JavaScript to separate files (could reduce template size)
   - Add Storybook or design system UI for component showcase
   - Implement CSS-in-JS or BEM naming for highly dynamic components
5. **Maintenance**:
   - Keep `docs/web-ui-components.md` updated as new components are added
   - Document any custom Tailwind plugins or extensions
   - Review color contrast annually as dark mode evolves

---

## Timeline Estimate

- **Phase 1**: 4–6 hours (setup, extraction, documentation)
- **Phase 2**: 8–10 hours (refactor all templates)
- **Phase 3**: 4–6 hours (design tokens, config updates)
- **Phase 4**: 2–3 hours (tier system)
- **Phase 5**: 6–8 hours (QA, testing, contrast checks)
- **Phase 6**: 2–4 hours (cleanup, docs, formatting)

**Total**: ~26–37 hours (3–5 days at 8h/day)

---

## Appendix: Current Component Classes Reference

| Class | Purpose | Location |
|-------|---------|----------|
| `.pill` | Small inline badge (watch, scan status) | tailwind.input.css:27 |
| `.pill-on` | Pill modifier: success state | tailwind.input.css:28 |
| `.pill-off` | Pill modifier: inactive state | tailwind.input.css:29 |
| `.badge-tier` | Tier classification badge | tailwind.input.css:30 |
| `.stat-card` | Dashboard stat display | tailwind.input.css:31 |
| `.card` | General content container | tailwind.input.css:32 |
| `.btn` | Base button (implied, needs definition) | Base in templates |
| `.btn-accent` | Primary action button (accent color) | tailwind.input.css:33 |
| `.btn-primary` | Primary action button | tailwind.input.css:35 |
| `.btn-success` | Positive action (green) | tailwind.input.css:36 |
| `.btn-warning` | Cautious action (orange) | tailwind.input.css:37 |
| `.btn-danger` | Destructive action (red) | tailwind.input.css:38 |
| `.btn-ghost` | Secondary action (outline) | tailwind.input.css:34 |
| `.btn-sm` | Small button variant | tailwind.input.css:39 |
| `.table-zebra` | Striped table rows | tailwind.input.css:40 |
| `.form-label` | Form label text | tailwind.input.css:41 |
| `.form-input` | Text input field | tailwind.input.css:42 |
| `.form-select` | Select dropdown | tailwind.input.css:43 |
| `.section-heading` | Section header wrapper | tailwind.input.css:44 |
| `.section-title` | Section title text | tailwind.input.css:45 |
| `.section-subtle` | Section subtitle text | tailwind.input.css:46 |
| `.modal-overlay` | Modal background overlay | tailwind.input.css:47 |
| `.modal-card` | Modal dialog box | tailwind.input.css:48 |
| `.chip` | Inline status indicator | tailwind.input.css:49 |

---

**Next Step**: Review this plan and approve or request changes before implementation begins.
