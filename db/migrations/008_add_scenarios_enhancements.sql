-- Migration 008: Scenario enhancements
-- Date: 2026-02-26
-- Description: Adds status workflow fields, visibility, batch run support

-- TestCase enhancements
ALTER TABLE testcase ADD COLUMN visibility VARCHAR DEFAULT 'public';
ALTER TABLE testcase ADD COLUMN approved_by INTEGER;
ALTER TABLE testcase ADD COLUMN test_case_number INTEGER;

-- TestRun batch/browser support
ALTER TABLE testrun ADD COLUMN batch_label VARCHAR;
ALTER TABLE testrun ADD COLUMN browser VARCHAR;

-- Project enhancements
ALTER TABLE project ADD COLUMN base_prompt VARCHAR;
ALTER TABLE project ADD COLUMN page_load_state VARCHAR DEFAULT 'load';
ALTER TABLE project ADD COLUMN test_case_prefix VARCHAR;
ALTER TABLE project ADD COLUMN next_test_case_number INTEGER DEFAULT 1;
