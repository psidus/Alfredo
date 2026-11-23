---
name: ui-ux-pro-max
category: design
description: Clean visual hierarchy, accessible contrast, responsive mobile/desktop layout, zero bloat.
scope: task
recommended_tools: [read_file, edit_file_diff]
min_model_tier: standard
---

# UI & UX Craft Guidelines

1. **Visual Consistency First**: Inherit the app's existing palette, spacing tokens, button classes, and card structure rather than inventing ad-hoc inline styles.
2. **Hierarchy & Scannability**: One primary CTA per view/card. Group secondary controls behind compact popovers, pills, or expanders so the screen never looks cluttered.
3. **Touch & Mobile Ergonomics**: Minimum 40px comfortable tap targets, zero horizontal overflow, clean Flex/Grid alignment (`align-items: center`, `min-height: 0` on scroll containers).
4. **Remove Dead & Duplicate Controls**: Eliminate redundant buttons or disconnected sections whenever two UI paths trigger the same backend capability.
5. **Contrast & State Feedback**: Ensure legible contrast (>= 4.5:1), clear disabled/loading/error states, and full bilingual i18n parity when supported.
