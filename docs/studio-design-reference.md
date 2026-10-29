# Studio design direction

Reviewed October 28, 2026. References are public product pages and documentation,
not an inspection of authenticated paid accounts. No third-party branding or
product assets are included in the application.

| Reference | Relevant pattern | Application here |
| --- | --- | --- |
| [Descript features](https://www.descript.com/features) and [editor layout guidance](https://help.descript.com/hc/en-us/articles/10164575118989-Auto-scrolling) | Text-oriented editing with adjustable workspace regions | Give the script a generous reading surface and separate workflow navigation from the editor on wide screens. |
| [Riverside](https://riverside.com/) | A visible progression through recording, editing, repurposing and publishing | Keep the numbered production stages accessible in a desktop side navigator; retain their established order and controls. |
| [Adobe Podcast](https://podcast.adobe.com/en/edit-audio-with-adobe-podcast) | A focused browser interface for audio editing | Use restrained surfaces, readable labels and grouped optional controls rather than decorative dashboard widgets. |

The visual interpretation uses a charcoal canvas, layered slate panels, restrained
green accents, clear headings and a consistent control scale. The wider desktop
layout adds working room without changing the document or render model. Below
1200px, navigation returns above the editor; below 700px it becomes a static
single-column list. Studio tabs retain their existing keyboard interaction.

Compact help buttons retain their original 20px circular geometry and tooltip
styles; the theme's large action-button rules exclude them. Tooltip IDs use a
page-local sequence so help creation does not require secure-context-only browser
APIs or prevent import initialization on HTTP deployments.

Acceptance criteria: all existing controls and IDs remain available, hidden panels
stay hidden, manual and JSON input remain available, and saved work, render
submission, polling, cancellation and downloads retain their behavior. No pricing,
subscription or third-party service integration is introduced by this refresh.
