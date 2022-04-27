import numpy as np
from collections.abc import Mapping
import sys
from . import fileio


def do_merge(output, files_to_merge, **kwargs):
    # Parse all stats files.
    stats = []
    for stat_file in files_to_merge:
        f = fileio.auto_open(
            stat_file,
            mode="r",
            nproc=kwargs.get("nproc_in"),
            command=kwargs.get("cmd_in", None),
        )
        # use a factory method to instanciate PairCounter
        stat = PairCounter.from_file(f)
        stats.append(stat)
        f.close()

    # combine stats from several files (files_to_merge):
    out_stat = sum(stats)

    # Save merged stats.
    outstream = fileio.auto_open(
        output,
        mode="w",
        nproc=kwargs.get("nproc_out"),
        command=kwargs.get("cmd_out", None),
    )

    # save statistics to file ...
    out_stat.save(outstream)

    if outstream != sys.stdout:
        outstream.close()


def estimate_library_complexity(nseq, ndup, nopticaldup=0):
    """Estimate library complexity accounting for optical/clustering duplicates
    Parameters
    ----------
    nseq : int
        Total number of sequences
    ndup : int
        Total number of duplicates
    nopticaldup : int, optional
        Number of non-PCR duplicates, by default 0
    Returns
    -------
    float
        Estimated complexity
    """
    nseq = nseq - nopticaldup
    if nseq == 0:
        warnings.warn("Empty of fully duplicated library, can't estimate complexity")
        return 0
    ndup = ndup - nopticaldup
    u = (nseq - ndup) / nseq
    seq_to_complexity = special.lambertw(-np.exp(-1 / u) / u).real + 1 / u
    complexity = nseq / seq_to_complexity
    return complexity


def extract_tile_info(series, regex=False):
    """Extract the name of the tile for each read name in the series
    Parameters
    ----------
    series : pd.Series
        Series containing read IDs
    regex : bool, optional
        Regex to extract fields from the read IDs that correspond to tile IDs.
        By default False, uses a faster predefined approach for typical Illumina
        read names
        Example: r"(?:\w+):(?:\w+):(\w+):(\w+):(\w+):(?:\w+):(?:\w+)"
    Returns
    -------
    Series
        Series containing tile IDs as strings
    """
    if regex:
        split = series.str.extractall(regex).unstack().droplevel(1, axis=1)
        return split[0] + ":" + split[1] + ":" + split[2]
    else:
        split = series.str.split(":", expand=True)
        return split[2] + ":" + split[3] + ":" + split[4]


def analyse_duplicate_stats(dups, tile_dup_regex=False):
    """Count by-tile duplicates
    Parameters
    ----------
    dups : pd.DataFrame
        Dataframe with duplicates that contains pared read IDs
    tile_dup_regex : bool, optional
        See extract_tile_info for details, by default False
    Returns
    -------
    pd.DataFrame
        Grouped multi-indexed dataframe of pairwise by-tile duplication counts
    """
    dups = dups.copy()
    dups["tile"] = extract_tile_info(dups["readID"])
    dups["parent_tile"] = extract_tile_info(dups["parent_readID"])
    dups["same_tile"] = dups["tile"] == dups["parent_tile"]
    bytile_dups = (
        dups.groupby(["tile", "parent_tile"])
        .size()
        .reset_index(name="dup_count")
        .sort_values(["tile", "parent_tile"])
    )
    bytile_dups[["tile", "parent_tile"]] = np.sort(
        bytile_dups[["tile", "parent_tile"]].values, axis=1
    )
    bytile_dups = bytile_dups.groupby(["tile", "parent_tile"]).sum()
    return bytile_dups


class PairCounter(Mapping):
    """
    A Counter for Hi-C pairs that accumulates various statistics.

    PairCounter implements two interfaces to access multi-level statistics:
    1. as a nested dict, e.g. pairCounter['pair_types']['LL']
    2. as a flat dict, with the level keys separated by '/', e.g. pairCounter['pair_types/LL']

    Other features:
    -- PairCounters can be saved into/loaded from a file
    -- multiple PairCounters can be merged via addition.
    """

    _SEP = "\t"
    _KEY_SEP = "/"

    def __init__(self, min_log10_dist=0, max_log10_dist=9, log10_dist_bin_step=0.25):
        self._stat = {}
        # some variables used for initialization:
        # genomic distance bining for the ++/--/-+/+- distribution
        self._dist_bins = np.r_[
            0,
            np.round(
                10
                ** np.arange(
                    min_log10_dist, max_log10_dist + 0.001, log10_dist_bin_step
                )
            ).astype(np.int),
        ]

        # establish structure of an empty _stat:
        self._stat["total"] = 0
        self._stat["total_unmapped"] = 0
        self._stat["total_single_sided_mapped"] = 0
        # total_mapped = total_dups + total_nodups
        self._stat["total_mapped"] = 0
        self._stat["total_dups"] = 0
        self._stat["total_nodups"] = 0
        ########################################
        # the rest of stats are based on nodups:
        ########################################
        self._stat["cis"] = 0
        self._stat["trans"] = 0
        self._stat["pair_types"] = {}
        # to be removed:
        self._stat["dedup"] = {}

        self._stat["cis_1kb+"] = 0
        self._stat["cis_2kb+"] = 0
        self._stat["cis_4kb+"] = 0
        self._stat["cis_10kb+"] = 0
        self._stat["cis_20kb+"] = 0
        self._stat["cis_40kb+"] = 0

        self._stat["chrom_freq"] = {}

        self._stat["dist_freq"] = {
            "+-": np.zeros(len(self._dist_bins), dtype=np.int),
            "-+": np.zeros(len(self._dist_bins), dtype=np.int),
            "--": np.zeros(len(self._dist_bins), dtype=np.int),
            "++": np.zeros(len(self._dist_bins), dtype=np.int),
        }

    def __getitem__(self, key):
        if isinstance(key, str):
            # let's strip any unintentional '/'
            # from either side of the key
            key = key.strip("/")
            if self._KEY_SEP in key:
                # multi-key to access nested elements
                k_fields = key.split(self._KEY_SEP)
            else:
                # single-key access flat part of PairCounter
                # or to access highest level of hierarchy
                return self._stat[key]
        else:
            # clearly an error:
            raise ValueError("{} is not a valid key: must be str".format(key))

        # K_FIELDS:
        # process multi-key case:
        # in this case key must be in ['pair_types','chrom_freq','dist_freq','dedup']
        # get the first 'k' and keep the remainders in 'k_fields'
        k = k_fields.pop(0)
        if k in ["pair_types", "dedup"]:
            # assert there is only one element in key_fields left:
            # 'pair_types' and 'dedup' treated the same
            if len(k_fields) == 1:
                return self._stat[k][k_fields[0]]
            else:
                raise ValueError(
                    "{} is not a valid key: {} section implies 1 identifier".format(
                        key, k
                    )
                )

        elif k == "chrom_freq":
            # assert remaining key_fields == [chr1, chr2]:
            if len(k_fields) == 2:
                return self._stat[k][tuple(k_fields)]
            else:
                raise ValueError(
                    "{} is not a valid key: {} section implies 2 identifiers".format(
                        key, k
                    )
                )

        elif k == "dist_freq":
            # assert that last element of key_fields is the 'directions'
            # THIS IS DONE FOR CONSISTENCY WITH .stats FILE
            # SHOULD THAT BE CHANGED IN .stats AND HERE AS WELL?
            if len(k_fields) == 2:
                # assert 'dirs' in ['++','--','+-','-+']
                dirs = k_fields.pop()
                # there is only genomic distance range of the bin that's left:
                (bin_range,) = k_fields
                # extract left border of the bin "1000000+" or "1500-6000":
                dist_bin_left = (
                    bin_range.strip("+")
                    if bin_range.endswith("+")
                    else bin_range.split("-")[0]
                )
                # get the index of that bin:
                bin_idx = (
                    np.searchsorted(self._dist_bins, int(dist_bin_left), "right") - 1
                )
                # store corresponding value:
                return self._stat["dist_freq"][dirs][bin_idx]
            else:
                raise ValueError(
                    "{} is not a valid key: {} section implies 2 identifiers".format(
                        key, k
                    )
                )
        else:
            raise ValueError("{} is not a valid key".format(k))

    def __iter__(self):
        return iter(self._stat)

    def __len__(self):
        return len(self._stat)

    def calculate_summaries(self):
        """calculate summary statistics (fraction of cis pairs at different cutoffs,
        complexity estimate) based on accumulated counts. Results are saved into
        self._stat['summary']
        """
        for cis_count in (
            "cis",
            "cis_1kb+",
            "cis_2kb+",
            "cis_4kb+",
            "cis_10kb+",
            "cis_20kb+",
            "cis_40kb+",
        ):
            self._stat["summary"][f"frac_{cis_count}"] = (
                (self._stat[cis_count] / self._stat["total_nodups"])
                if self._stat["total_nodups"] > 0
                else 0
            )
        self._stat["summary"]["frac_dups"] = (
            (self._stat["total_dups"] / self._stat["total_mapped"])
            if self._stat["total_mapped"] > 0
            else 0
        )
        self._stat["summary"]["complexity_naive"] = estimate_library_complexity(
            self._stat["total_mapped"], self._stat["total_dups"], 0
        )
        if self.bytile_dups.shape[0] > 0:
            self._stat["dups_by_tile_median"] = (
                self.bytile_dups["dup_count"].median() * self.bytile_dups.shape[0]
            )
        if "dups_by_tile_median" in self._stat:
            self._stat["summary"][
                "complexity_dups_by_tile_median"
            ] = estimate_library_complexity(
                self._stat["total_mapped"],
                self._stat["total_dups"],
                self._stat["dups_by_tile_median"],
            )

    @classmethod
    def from_file(cls, file_handle):
        """create instance of PairCounter from file

        Parameters
        ----------
        file_handle: file handle

        Returns
        -------
        PairCounter
            new PairCounter filled with the contents of the input file
        """
        # fill in from file - file_handle:
        stat_from_file = cls()
        for l in file_handle:
            fields = l.strip().split(cls._SEP)
            if len(fields) == 0:
                # skip empty lines:
                continue
            if len(fields) != 2:
                # expect two _SEP separated values per line:
                raise fileio.ParseError(
                    "{} is not a valid stats file".format(file_handle.name)
                )
            # extract key and value, then split the key:
            putative_key, putative_val = fields[0], fields[1]
            key_fields = putative_key.split(cls._KEY_SEP)
            # we should impose a rigid structure of .stats or redo it:
            if len(key_fields) == 1:
                key = key_fields[0]
                if key in stat_from_file._stat:
                    stat_from_file._stat[key] = int(fields[1])
                else:
                    raise fileio.ParseError(
                        "{} is not a valid stats file: unknown field {} detected".format(
                            file_handle.name, key
                        )
                    )
            else:
                # in this case key must be in ['pair_types','chrom_freq','dist_freq','dedup']
                # get the first 'key' and keep the remainders in 'key_fields'
                key = key_fields.pop(0)
                if key in ["pair_types", "dedup"]:
                    # assert there is only one element in key_fields left:
                    # 'pair_types' and 'dedup' treated the same
                    if len(key_fields) == 1:
                        stat_from_file._stat[key][key_fields[0]] = int(fields[1])
                    else:
                        raise fileio.ParseError(
                            "{} is not a valid stats file: {} section implies 1 identifier".format(
                                file_handle.name, key
                            )
                        )

                elif key == "chrom_freq":
                    # assert remaining key_fields == [chr1, chr2]:
                    if len(key_fields) == 2:
                        stat_from_file._stat[key][tuple(key_fields)] = int(fields[1])
                    else:
                        raise fileio.ParseError(
                            "{} is not a valid stats file: {} section implies 2 identifiers".format(
                                file_handle.name, key
                            )
                        )

                elif key == "dist_freq":
                    # assert that last element of key_fields is the 'directions'
                    if len(key_fields) == 2:
                        # assert 'dirs' in ['++','--','+-','-+']
                        dirs = key_fields.pop()
                        # there is only genomic distance range of the bin that's left:
                        (bin_range,) = key_fields
                        # extract left border of the bin "1000000+" or "1500-6000":
                        dist_bin_left = (
                            bin_range.strip("+")
                            if bin_range.endswith("+")
                            else bin_range.split("-")[0]
                        )
                        # get the index of that bin:
                        bin_idx = (
                            np.searchsorted(
                                stat_from_file._dist_bins, int(dist_bin_left), "right"
                            )
                            - 1
                        )
                        # store corresponding value:
                        stat_from_file._stat[key][dirs][bin_idx] = int(fields[1])
                    else:
                        raise fileio.ParseError(
                            "{} is not a valid stats file: {} section implies 2 identifiers".format(
                                file_handle.name, key
                            )
                        )
                else:
                    raise fileio.ParseError(
                        "{} is not a valid stats file: unknown field {} detected".format(
                            file_handle.name, key
                        )
                    )
        # return PairCounter from a non-empty dict:
        return stat_from_file

    def add_pair(self, chrom1, pos1, strand1, chrom2, pos2, strand2, pair_type):
        """Gather statistics for a Hi-C pair and add to the PairCounter.

        Parameters
        ----------
        chrom1: str
            chromosome of the first read
        pos1: int
            position of the first read
        strand1: str
            strand of the first read
        chrom2: str
            chromosome of the first read
        pos2: int
            position of the first read
        strand2: str
            strand of the first read
        pair_type: str
            type of the mapped pair of reads
        """

        self._stat["total"] += 1
        # collect pair type stats including DD:
        self._stat["pair_types"][pair_type] = (
            self._stat["pair_types"].get(pair_type, 0) + 1
        )
        if chrom1 == "!" and chrom2 == "!":
            self._stat["total_unmapped"] += 1
        elif chrom1 != "!" and chrom2 != "!":
            self._stat["total_mapped"] += 1
            # only mapped ones can be duplicates:
            if pair_type == "DD":
                self._stat["total_dups"] += 1
            else:
                self._stat["total_nodups"] += 1
                self._stat["chrom_freq"][(chrom1, chrom2)] = (
                    self._stat["chrom_freq"].get((chrom1, chrom2), 0) + 1
                )

                if chrom1 == chrom2:
                    self._stat["cis"] += 1
                    dist = np.abs(pos2 - pos1)
                    bin_idx = np.searchsorted(self._dist_bins, dist, "right") - 1
                    self._stat["dist_freq"][strand1 + strand2][bin_idx] += 1
                    if dist >= 1000:
                        self._stat["cis_1kb+"] += 1
                    if dist >= 2000:
                        self._stat["cis_2kb+"] += 1
                    if dist >= 4000:
                        self._stat["cis_4kb+"] += 1
                    if dist >= 10000:
                        self._stat["cis_10kb+"] += 1
                    if dist >= 20000:
                        self._stat["cis_20kb+"] += 1
                    if dist >= 40000:
                        self._stat["cis_40kb+"] += 1

                else:
                    self._stat["trans"] += 1
        else:
            self._stat["total_single_sided_mapped"] += 1

    def add_pairs_from_dataframe(self, df, unmapped_chrom="!"):
        """Gather statistics for Hi-C pairs in a dataframe and add to the PairCounter.

        Parameters
        ----------
        df: pd.DataFrame
            DataFrame with pairs. Needs to have columns:
                'chrom1', 'pos1', 'chrom2', 'pos2', 'strand1', 'strand2', 'pair_type'
        """

        total_count = df.shape[0]
        self._stat["total"] += total_count

        # collect pair type stats including DD:
        for pair_type, type_count in df["pair_type"].value_counts().items():
            self._stat["pair_types"][pair_type] = (
                self._stat["pair_types"].get(pair_type, 0) + type_count
            )

        # Count the unmapped by the "unmapped" chromosomes (debatable, as WW are also marked as ! and they might be mapped):
        unmapped_count = np.logical_and(
            df["chrom1"] == unmapped_chrom, df["chrom2"] == unmapped_chrom
        ).sum()
        self._stat["total_unmapped"] += int(unmapped_count)

        # Count the mapped:
        df_mapped = df.loc[
            (df["chrom1"] != unmapped_chrom) & (df["chrom2"] != unmapped_chrom), :
        ]
        mapped_count = df_mapped.shape[0]

        self._stat["total_mapped"] += mapped_count
        self._stat["total_single_sided_mapped"] += int(
            total_count - (mapped_count + unmapped_count)
        )

        # Count the duplicates:
        if "duplicate" in df_mapped.columns:
            mask_dups = df_mapped["duplicate"]
        else:
            mask_dups = df_mapped["pair_type"] == "DD"
        dups_count = mask_dups.sum()
        self._stat["total_dups"] += int(dups_count)
        self._stat["total_nodups"] += int(mapped_count - dups_count)

        # Count pairs per chromosome:
        for (chrom1, chrom2), chrom_count in (
            df_mapped[["chrom1", "chrom2"]].value_counts().items()
        ):
            self._stat["chrom_freq"][(chrom1, chrom2)] = (
                self._stat["chrom_freq"].get((chrom1, chrom2), 0) + chrom_count
            )

        # Count cis-trans by pairs:
        df_nodups = df_mapped.loc[~mask_dups, :]
        mask_cis = df_nodups["chrom1"] == df_nodups["chrom2"]
        df_cis = df_nodups.loc[mask_cis, :].copy()
        self._stat["cis"] += df_cis.shape[0]
        self._stat["trans"] += df_nodups.shape[0] - df_cis.shape[0]
        dist = np.abs(df_cis["pos2"].values - df_cis["pos1"].values)

        df_cis.loc[:, "bin_idx"] = np.searchsorted(self._dist_bins, dist, "right") - 1
        for (strand1, strand2, bin_id), strand_bin_count in (
            df_cis[["strand1", "strand2", "bin_idx"]].value_counts().items()
        ):
            self._stat["dist_freq"][strand1 + strand2][bin_id] += strand_bin_count
        self._stat["cis_1kb+"] += int(np.sum(dist >= 1000))
        self._stat["cis_2kb+"] += int(np.sum(dist >= 2000))
        self._stat["cis_4kb+"] += int(np.sum(dist >= 4000))
        self._stat["cis_10kb+"] += int(np.sum(dist >= 10000))
        self._stat["cis_20kb+"] += int(np.sum(dist >= 20000))
        self._stat["cis_40kb+"] += int(np.sum(dist >= 40000))

    def add_chromsizes(self, chromsizes):
        """ Add chromsizes field to the output stats

        Parameters
        ----------
        chromsizes: Dataframe with chromsizes, read by headerops.chromsizes
        """

        chromsizes = chromsizes.to_dict()
        self._stat["chromsizes"] = chromsizes
        return

    def __add__(self, other):
        # both PairCounter are implied to have a list of common fields:
        #
        # 'total', 'total_unmapped', 'total_single_sided_mapped', 'total_mapped',
        # 'cis', 'trans', 'pair_types', 'cis_1kb+', 'cis_2kb+',
        # 'cis_10kb+', 'cis_20kb+', 'chrom_freq', 'dist_freq', 'dedup'
        #
        # initialize empty PairCounter for the result of summation:
        sum_stat = PairCounter()
        # use the empty PairCounter to iterate over:
        for k, v in sum_stat._stat.items():
            # not nested fields are summed trivially:
            if isinstance(v, int):
                sum_stat._stat[k] = self._stat[k] + other._stat[k]
            # sum nested dicts/arrays in a context dependet manner:
            else:
                if k in ["pair_types", "dedup"]:
                    # handy function for summation of a pair of dicts:
                    # https://stackoverflow.com/questions/10461531/merge-and-sum-of-two-dictionaries
                    sum_dicts = lambda dict_x, dict_y: {
                        key: dict_x.get(key, 0) + dict_y.get(key, 0)
                        for key in set(dict_x) | set(dict_y)
                    }
                    # sum a pair of corresponding dicts:
                    sum_stat._stat[k] = sum_dicts(self._stat[k], other._stat[k])
                if k == "chrom_freq":
                    # union list of keys (chr1,chr2) with potential duplicates:
                    union_keys_with_dups = list(self._stat[k].keys()) + list(
                        other._stat[k].keys()
                    )
                    # dict.fromkeys will take care of keys' order and duplicates in a consistent manner:
                    # https://stackoverflow.com/questions/1720421/how-to-concatenate-two-lists-in-python
                    # last comment to the 3rd Answer
                    sum_stat._stat[k] = dict.fromkeys(union_keys_with_dups)
                    # perform a summation:
                    for union_key in sum_stat._stat[k]:
                        sum_stat._stat[k][union_key] = self._stat[k].get(
                            union_key, 0
                        ) + other._stat[k].get(union_key, 0)
                if k == "dist_freq":
                    for dirs in sum_stat[k]:
                        sum_stat[k][dirs] = self._stat[k][dirs] + other._stat[k][dirs]
        return sum_stat

    # we need this to be able to sum(list_of_PairCounters)
    def __read__(self, other):
        if other == 0:
            return self
        else:
            return self.__add__(other)

    def flatten(self):
        """return a flattened dict (formatted same way as .stats file)"""
        # dict for flat store:
        flat_stat = {}

        # Storing statistics
        for k, v in self._stat.items():
            if isinstance(v, int):
                flat_stat[k] = v
            # store nested dicts/arrays in a context dependet manner:
            # nested categories are stored only if they are non-trivial
            else:
                if (k == "dist_freq") and v:
                    for i in range(len(self._dist_bins)):
                        for dirs, freqs in v.items():
                            # last bin is treated differently: "100000+" vs "1200-3000":
                            if i != len(self._dist_bins) - 1:
                                formatted_key = self._KEY_SEP.join(
                                    ["{}", "{}-{}", "{}"]
                                ).format(
                                    k, self._dist_bins[i], self._dist_bins[i + 1], dirs
                                )
                            else:
                                formatted_key = self._KEY_SEP.join(
                                    ["{}", "{}+", "{}"]
                                ).format(k, self._dist_bins[i], dirs)
                            # store key,value pair:
                            flat_stat[formatted_key] = freqs[i]
                elif (k in ["pair_types", "dedup", "chromsizes"]) and v:
                    # 'pair_types' and 'dedup' are simple dicts inside,
                    # treat them the exact same way:
                    for k_item, freq in v.items():
                        formatted_key = self._KEY_SEP.join(["{}", "{}"]).format(
                            k, k_item
                        )
                        # store key,value pair:
                        flat_stat[formatted_key] = freq
                elif (k == "chrom_freq") and v:
                    for (chrom1, chrom2), freq in v.items():
                        formatted_key = self._KEY_SEP.join(["{}", "{}", "{}"]).format(
                            k, chrom1, chrom2
                        )
                        # store key,value pair:
                        flat_stat[formatted_key] = freq

        # return flattened dict
        return flat_stat

    def format(self):
        """return a formatted dict (for the yaml output)"""

        from copy import deepcopy

        formatted_stat = {}

        # Storing statistics
        for k, v in self._stat.items():
            if isinstance(v, int):
                formatted_stat[k] = v
            # store nested dicts/arrays in a context dependet manner:
            # nested categories are stored only if they are non-trivial
            else:
                if (k == "dist_freq") and v:
                    freqs_dct = {}

                    # iterate over distance bins:
                    for i in range(len(self._dist_bins)):
                        # iterate over all directions:
                        for dirs, freqs in v.items():
                            # last bin is treated differently: "100000+" vs "1200-3000":
                            if i != len(self._dist_bins) - 1:
                                dist = "{}-{}".format(
                                    self._dist_bins[i], self._dist_bins[i + 1]
                                )
                            else:
                                dist = "{}+".format(self._dist_bins[i])
                            if dist not in freqs_dct.keys():
                                freqs_dct[dist] = {}

                            freqs_dct[dist][dirs] = int(freqs[i])

                    formatted_stat[k] = deepcopy(freqs_dct)

                elif (k in ["pair_types", "dedup", "chromsizes"]) and v:
                    # 'pair_types' and 'dedup' are simple dicts inside,
                    # treat them the exact same way:
                    formatted_stat[k] = deepcopy(v)
                elif (k == "chrom_freq") and v:
                    freqs = {}
                    for (chrom1, chrom2), freq in v.items():
                        freqs[
                            self._KEY_SEP.join(["{}", "{}"]).format(chrom1, chrom2)
                        ] = freq
                        # store key,value pair:
                    formatted_stat[k] = deepcopy(freqs)

        # return formatted dict
        return formatted_stat

    def save(self, outstream, yaml=False):
        """save PairCounter to tab-delimited text file.
        Flattened version of PairCounter is stored in the file.

        Parameters
        ----------
        outstream: file handle
        yaml: is output in yaml format or table

        Note
        ----
        The order of the keys is not guaranteed
        Merging several .stats is not associative with respect to key order:
        merge(A,merge(B,C)) != merge(merge(A,B),C).

        Theys shou5ld match exactly, however, when soprted:
        sort(merge(A,merge(B,C))) == sort(merge(merge(A,B),C))
        """

        # write flattened version of the PairCounter to outstream
        if yaml:
            import yaml

            data = self.format()
            yaml.dump(data, outstream, default_flow_style=False)
        else:
            for k, v in self.flatten().items():
                outstream.write("{}{}{}\n".format(k, self._SEP, v))
