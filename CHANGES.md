### 0.3.1 (2021-02-XX) ###

* sample: a new tool to select a random subset of pairs
* parse: add --readid-transform to edit readID
* parse: add experimental --walk-policy all (note: it will be moved 
  to a separate tool in future!) 
* all tools: use bgzip if pbgzip not available

Internal changes:
* parse: move most code to a separate _parse module
* _headerops: add extract_chromosomes(header)  
* all tools: drop py3.5 support
* switch from travis CI to github actions

### 0.3.0 (2019-04-23) ###

* parse: tag pairs with missing FASTQ/SAM on one side as corrupt, pair type "XX"

### 0.2.2 (2019-01-07) ###

* sort: enable lz4c compression of sorted chunks by default

### 0.2.1 (2018-12-21) ###

* automatically convert mapq1 and mapq2 to int in `select`

### 0.2.0 (2018-09-03) ###

* add the `flip` tool

### 0.1.1 (2018-07-19) ###

* Bugfix: include _dedup.pyx in the Python package

### 0.1.0 (2018-07-19) ###

* First release.
