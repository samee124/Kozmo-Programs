-- ============================================================
-- Migration: Add sequential integer ID columns
-- Run this once in SQL Server Management Studio (SSMS)
-- against your Cobalt database.
--
-- Adds a SeqId INT IDENTITY(1,1) column to ProgrammeRun and
-- VendorIntelligence so each row has a human-readable integer
-- display ID (1, 2, 3 ...) alongside the string natural keys.
-- ============================================================

-- ProgrammeRun: add SeqId
IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('ProgrammeRun') AND name = 'SeqId'
)
BEGIN
    ALTER TABLE ProgrammeRun ADD SeqId INT IDENTITY(1,1) NOT NULL;
    PRINT 'Added SeqId to ProgrammeRun';
END
ELSE
    PRINT 'SeqId already exists on ProgrammeRun — skipped';

-- VendorIntelligence: add SeqId
IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('VendorIntelligence') AND name = 'SeqId'
)
BEGIN
    ALTER TABLE VendorIntelligence ADD SeqId INT IDENTITY(1,1) NOT NULL;
    PRINT 'Added SeqId to VendorIntelligence';
END
ELSE
    PRINT 'SeqId already exists on VendorIntelligence — skipped';

-- Optional: make SeqId a unique index for fast lookup
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'UQ_ProgrammeRun_SeqId')
    CREATE UNIQUE INDEX UQ_ProgrammeRun_SeqId ON ProgrammeRun(SeqId);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'UQ_VendorIntelligence_SeqId')
    CREATE UNIQUE INDEX UQ_VendorIntelligence_SeqId ON VendorIntelligence(SeqId);

PRINT 'Migration complete.';
