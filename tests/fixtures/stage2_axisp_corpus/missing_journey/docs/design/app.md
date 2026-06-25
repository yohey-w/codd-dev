---
codd:
  # Three actors are declared...
  actors:
    - Learner
    - Admin
    - Instructor
  # ...but only two of them get a journey. "Admin" is left with no journey,
  # which is the construction-derived gap recorded in gold.yaml.
  user_journeys:
    - name: learner_views_course
      actor: Learner
      criticality: critical
      required_capabilities:
        - read_course
      steps:
        - action: navigate
          target: /course
        - action: assert
          value: course_visible
    - name: instructor_grades_submission
      actor: Instructor
      criticality: high
      required_capabilities:
        - grade_submission
      steps:
        - action: navigate
          target: /grade
        - action: assert
          value: grade_saved
---

# App design

Learner and Instructor have declared journeys. Admin does not — that is the
intentional positive-coverage gap for this fixture.
