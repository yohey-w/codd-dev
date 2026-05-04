# Project Lexicon Questions

Use these questions to create a project-specific `project_lexicon.yaml`.
The questions are intentionally framework-neutral and should be answered with the vocabulary and naming rules used by the current project.

### Q01: URL path naming
**カテゴリ**: url_route
**質問**: Project URL paths should use which naming convention?
**選択肢**: kebab-case / snake_case / camelCase / other
**デフォルト**: kebab-case
**lexicon生成先**: node_vocabulary[type=url_route].naming_convention

### Q02: URL parameter naming
**カテゴリ**: url_route
**質問**: Dynamic URL parameters should use which naming convention?
**選択肢**: snake_case / camelCase / kebab-case / other
**デフォルト**: snake_case
**lexicon生成先**: node_vocabulary[type=url_route].parameter_naming

### Q03: URL role prefixes
**カテゴリ**: url_route
**質問**: Do URL paths have role or area prefixes?
**選択肢**: none / role=/prefix pairs / area=/prefix pairs / other
**デフォルト**: none
**lexicon生成先**: node_vocabulary[type=url_route].prefix_rules

### Q04: API resource plurality
**カテゴリ**: url_route
**質問**: Should API resources use plural or singular nouns?
**選択肢**: plural / singular / mixed by domain / other
**デフォルト**: plural
**lexicon生成先**: node_vocabulary[type=url_route].plurality

### Q05: DB table plurality
**カテゴリ**: db_model
**質問**: Should DB table names use plural or singular nouns?
**選択肢**: plural / singular / mixed by domain / other
**デフォルト**: plural
**lexicon生成先**: node_vocabulary[type=db_table].plurality

### Q06: DB model naming
**カテゴリ**: db_model
**質問**: Application-level DB model names should use which naming convention?
**選択肢**: PascalCase / snake_case / camelCase / other
**デフォルト**: PascalCase
**lexicon生成先**: node_vocabulary[type=db_model].naming_convention

### Q07: DB column naming
**カテゴリ**: db_model
**質問**: DB column names should use which naming convention?
**選択肢**: snake_case / camelCase / SCREAMING_SNAKE_CASE / other
**デフォルト**: snake_case
**lexicon生成先**: node_vocabulary[type=db_column].naming_convention

### Q08: Environment variable naming
**カテゴリ**: env_var
**質問**: Environment variables should use which naming convention?
**選択肢**: SCREAMING_SNAKE_CASE / snake_case / camelCase / other
**デフォルト**: SCREAMING_SNAKE_CASE
**lexicon生成先**: node_vocabulary[type=env_var].naming_convention

### Q09: Environment variable prefixes
**カテゴリ**: env_var
**質問**: Do environment variables require a project, service, or runtime prefix?
**選択肢**: none / project prefix / service prefix / runtime prefix / other
**デフォルト**: none
**lexicon生成先**: node_vocabulary[type=env_var].prefix_rules

### Q10: Secret naming
**カテゴリ**: env_var
**質問**: Secret names should use which naming convention?
**選択肢**: SCREAMING_SNAKE_CASE / snake_case / kebab-case / other
**デフォルト**: SCREAMING_SNAKE_CASE
**lexicon生成先**: node_vocabulary[type=secret].naming_convention

### Q11: CLI command naming
**カテゴリ**: cli_command
**質問**: CLI commands and subcommands should use which naming convention?
**選択肢**: kebab-case / snake_case / camelCase / other
**デフォルト**: kebab-case
**lexicon生成先**: node_vocabulary[type=cli_command].naming_convention

### Q12: CLI option naming
**カテゴリ**: cli_command
**質問**: CLI flags and options should use which naming convention?
**選択肢**: kebab-case / snake_case / camelCase / other
**デフォルト**: kebab-case
**lexicon生成先**: node_vocabulary[type=cli_option].naming_convention

### Q13: CLI namespace prefixes
**カテゴリ**: cli_command
**質問**: Do CLI commands require project, module, or mode prefixes?
**選択肢**: none / project prefix / module prefix / mode prefix / other
**デフォルト**: none
**lexicon生成先**: node_vocabulary[type=cli_command].prefix_rules

### Q14: Role and permission prefixes
**カテゴリ**: role_permission
**質問**: If the system has roles, what URL, command, or policy prefixes map to each role?
**選択肢**: none / role=/prefix pairs / role=permission-prefix pairs / other
**デフォルト**: none
**lexicon生成先**: node_vocabulary[type=role].prefix_rules

### Q15: Permission key naming
**カテゴリ**: role_permission
**質問**: Permission keys should use which naming convention?
**選択肢**: snake_case / kebab-case / camelCase / other
**デフォルト**: snake_case
**lexicon生成先**: node_vocabulary[type=permission].naming_convention

### Q16: Domain event naming
**カテゴリ**: domain_event
**質問**: Domain event names should use which naming convention?
**選択肢**: snake_case / PascalCase / kebab-case / other
**デフォルト**: snake_case
**lexicon生成先**: node_vocabulary[type=domain_event].naming_convention

### Q17: Domain event tense
**カテゴリ**: domain_event
**質問**: Should domain events be named in past tense, imperative form, or state form?
**選択肢**: past_tense / imperative / state / other
**デフォルト**: past_tense
**lexicon生成先**: node_vocabulary[type=domain_event].tense

### Q18: Component naming
**カテゴリ**: component_module
**質問**: UI or runtime component names should use which naming convention?
**選択肢**: PascalCase / kebab-case / snake_case / other
**デフォルト**: PascalCase
**lexicon生成先**: node_vocabulary[type=component].naming_convention

### Q19: Module file naming
**カテゴリ**: component_module
**質問**: Module or source file names should use which naming convention?
**選択肢**: snake_case / kebab-case / camelCase / PascalCase / other
**デフォルト**: snake_case
**lexicon生成先**: node_vocabulary[type=module_file].naming_convention

### Q20: Service and use case naming
**カテゴリ**: component_module
**質問**: Service, use case, or application operation names should use which naming convention?
**選択肢**: PascalCase / camelCase / snake_case / other
**デフォルト**: PascalCase
**lexicon生成先**: node_vocabulary[type=service_usecase].naming_convention

### Q21: Cross-artifact naming principle
**カテゴリ**: design_principle
**質問**: What project-wide rule should CoDD follow when a concept appears in code, docs, config, and CLI?
**選択肢**: one canonical name / allow local aliases / document exceptions / other
**デフォルト**: Use one canonical name per domain concept across code, docs, config, and CLI.
**lexicon生成先**: design_principles[]
