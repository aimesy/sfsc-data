# Harvard Dataverse publication plan

## Publication model

Publish a compact research release from a separate lightweight GitHub repository,
such as `aimesy/sfsc-dataverse`. A weekly workflow in that repository should copy
approved exports from `aimesy/sfsc-data`, commit only the publication package, and
then run `IQSS/dataverse-uploader@v1.7` against the existing Harvard Dataverse DOI.

Do not run the uploader directly from the main `sfsc-data` branch. The action
performs its own complete checkout even when `GITHUB_DIR` limits the upload. That
checkout is unsuitable for the large operational archive.

The uploader also does not create a Dataverse dataset or edit citation metadata.
Create and review the dataset in Harvard Dataverse first, then store its DOI as the
GitHub variable `DATAVERSE_DATASET_DOI` and its API token as the GitHub secret
`DATAVERSE_TOKEN` in the lightweight publication repository.

Keep automatic uploads as drafts until the package and metadata have passed a
privacy review. Publishing a changed file set creates a new major dataset version.

## Recommended Dataverse metadata

- **Title:** San Francisco Superior Court Research Data
- **Dataset type:** Dataset
- **Subjects:** Law; Social Sciences
- **Keywords:** San Francisco Superior Court; California courts; court records;
  register of actions; court dockets; tentative rulings; civil litigation;
  criminal cases; probate proceedings; judicial decisions; legal analytics;
  public records; empirical legal studies
- **Description:** Deidentified research tables and aggregate measures derived
  from public records concerning San Francisco Superior Court cases, dockets,
  tentative rulings, proceedings, and outcomes. Coverage and completeness vary
  by source, case type, department, and time period. This is an independent
  research archive and not an official court system.
- **Geographic coverage:** San Francisco County, California, United States
- **Language:** English
- **Data sources:** San Francisco Superior Court public records and related public
  sources
- **Related materials:** `https://github.com/aimesy/sfsc-data` and
  `https://sfsc.amyc.us/`
- **License:** Custom Terms, using the current `DATA-USE-TERMS.md` included in each publication package and linked from the dataset metadata
- **Citation requirement:** Cite the Dataverse dataset and version DOI. When
  feasible, also cite the `sfsc-data` commit recorded in the release manifest.

Add the depositor's preferred author name, affiliation, contact email, and ORCID
in Harvard Dataverse. Do not place those values in automation until they have been
confirmed by the depositor.

## Initial publication package

The first release should contain only deidentified or aggregate products:

1. A data dictionary and methodology note.
2. A release manifest with the source commit, generation time, row counts,
   coverage dates, and SHA-256 checksums.
3. Monthly case filing counts by broad case type.
4. Monthly docket activity counts by broad case type and event category.
5. Case duration and disposition summaries by filing cohort and broad case type.
6. Tentative ruling counts and disposition summaries by month, department, and
   broad motion category.
7. Document availability and OCR coverage statistics by month and case type.
8. Aggregate representation statistics, without party, litigant, or attorney
   names or stable person identifiers.
9. Aggregate probate event and role statistics.
10. Aggregate criminal charge and disposition statistics using broad charge
    categories and minimum cell size suppression.

Exclude raw case JSON, case titles, case numbers, party and litigant tables,
addresses, document bytes, OCR text, source URLs containing stable identifiers,
and small cells that could identify a person until Harvard's privacy requirements
have been reviewed against each product.

## Uploader workflow for the publication repository

```yaml
name: Publish SFSC research data to Harvard Dataverse

on:
  workflow_dispatch:
    inputs:
      publish:
        description: Publish the uploaded draft as a new major version
        type: boolean
        default: false
  schedule:
    - cron: '17 11 * * 1'

permissions:
  contents: read

concurrency:
  group: harvard-dataverse
  cancel-in-progress: false

jobs:
  upload:
    runs-on: ubuntu-latest
    steps:
      - name: Upload publication package
        uses: IQSS/dataverse-uploader@v1.7
        with:
          DATAVERSE_TOKEN: ${{ secrets.DATAVERSE_TOKEN }}
          DATAVERSE_SERVER: https://dataverse.harvard.edu
          DATAVERSE_DATASET_DOI: ${{ vars.DATAVERSE_DATASET_DOI }}
          GITHUB_DIR: data
          DELETE: true
          PUBLISH: ${{ github.event_name == 'workflow_dispatch' && inputs.publish || false }}
```

The scheduled run refreshes the Dataverse draft. Publication remains an explicit
manual action so incomplete exports are not assigned a public version DOI.
