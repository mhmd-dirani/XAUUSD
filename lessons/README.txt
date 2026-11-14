TEACHING LESSONS FOR ASTRA
==========================
One lesson = a chart image + a .txt file with the SAME name (no dates needed):

  001_rejection.png
  001_rejection.txt

Inside the .txt:

  QUESTION: What do you see here? Would you buy or sell?
  MY VIEW: Sell. This area rejected 3 times and equal highs sit just above (liquidity).
  I only sell after a sweep above the highs and a close back below the area.

- Astra answers your QUESTION blind first, then reads MY VIEW and learns your principles.
- Labelled teaching chart? Put  TYPE: CONCEPT  as the first line: Astra explains your marks
  blind, then stores your definitions (FVG, OB, MSS...) as DEFINITION principles.
- Text-only lessons (a .txt without an image) also work.
- Then run, from the project root:
      .\.venv\Scripts\python.exe tools\learn_lessons.py
- Read LESSONS_REPORT.md. The live bot uses astra_playbook.json from its next hourly analysis.
- Already-studied lessons are skipped; edit a lesson's files and it is studied again.
- Delete astra_playbook.json to stop Astra using the principles.
