-- The city/network intersection has a place and network label, including a
-- nullable ASN when enrichment could not classify the observed exit.
ALTER TABLE asn_labels ALTER COLUMN asn DROP NOT NULL;
ALTER TABLE calculated_breakdowns DROP CONSTRAINT calculated_breakdowns_dimension_check;
ALTER TABLE calculated_breakdowns ADD CONSTRAINT calculated_breakdowns_dimension_check
 CHECK(dimension IN ('g','c','a','r','x','y'));
