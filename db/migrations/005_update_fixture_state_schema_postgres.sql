-- Migration: Update fixture state schema for Playwright storage_state (PostgreSQL)
-- Date: 2026-02-06
-- Description: Replace separate encrypted fields with url and encrypted_state_json

-- PostgreSQL version: Use ALTER TABLE to modify columns

DO $$
BEGIN
    -- Add new columns if they don't exist
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'fixturestate' AND column_name = 'url'
    ) THEN
        ALTER TABLE fixturestate ADD COLUMN url VARCHAR;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'fixturestate' AND column_name = 'encrypted_state_json'
    ) THEN
        ALTER TABLE fixturestate ADD COLUMN encrypted_state_json VARCHAR;
    END IF;

    -- Drop old columns if they exist
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'fixturestate' AND column_name = 'encrypted_cookies'
    ) THEN
        ALTER TABLE fixturestate DROP COLUMN encrypted_cookies;
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'fixturestate' AND column_name = 'encrypted_local_storage'
    ) THEN
        ALTER TABLE fixturestate DROP COLUMN encrypted_local_storage;
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'fixturestate' AND column_name = 'encrypted_session_storage'
    ) THEN
        ALTER TABLE fixturestate DROP COLUMN encrypted_session_storage;
    END IF;
END $$;
