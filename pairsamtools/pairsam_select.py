import sys
import click
import re, fnmatch

from . import _fileio, _pairsam_format, cli, _headerops

UTIL_NAME = 'pairsam_select'

@cli.command()
@click.argument(
    'condition',
    type=str
)

@click.argument(
    'pairsam_path', 
    type=str,
    required=False)


@click.option(
    '-o', "--output", 
    type=str, 
    default="", 
    help='output file.'
        ' If the path ends with .gz, the output is bgzip-compressed.'
        ' By default, the output is printed into stdout.')

@click.option(
    "--output-rest", 
    type=str, 
    default="", 
    help='output file for pairs of other types. '
        ' If the path ends with .gz, the output is bgzip-compressed.'
        ' By default, such pairs are dropped.')

@click.option(
    "--send-comments-to", 
    type=click.Choice(['selected', 'rest', 'both', 'none']),
    default="both", 
    help="Which of the outputs should receive header and comment lines",
    show_default=True)

def select(
    condition, pairsam_path, output, output_rest, send_comments_to
    ):
    '''select pairsam entries.

    CONDITION : A Python expression; if it returns True, select the read pair.
    Any column declared in the #columns line of the pairs header can be 
    accessed by its name. If the header lacks the #columns line, the columns
    are assumed to follow the pairs/pairsam standard (readID, chrom1, chrom2, 
    pos1, pos2, strand1, strand2, pair_type). Finally, CONDITION has access to 
    COLS list which contains the string values of columns. In Bash, quote 
    CONDITION with single quotes, and use double quotes for string variables
    inside CONDITION.

    PAIRSAM_PATH : input .pairsam file. If the path ends with .gz, the input is
    gzip-decompressed. By default, the input is read from stdin.

    The following functions can be used in CONDITION besides the standard Python functions:

    - csv_match(x, csv) - True if variable x is contained in a list of
    comma-separated values, e.g. csv_match(chrom1, 'chr1,chr2')

    - wildcard_match(x, wildcard) - True if variable x matches a wildcard,
    e.g. wildcard_match(pair_type, 'C*')

    - regex_match(x, regex) - True if variable x matches a Python-flavor regex,
    e.g. regex_match(chrom1, 'chr\d')

    \b
    Examples:
    pairsam select '(pair_type=="LL") or (pair_type=="CX")'
    pairsam select 'chrom1==chrom2'
    pairsam select 'COLS[1]==COLS[3]'
    pairsam select '(chrom1==chrom2) and (abs(pos1 - pos2) < 1e6)'
    pairsam select '(chrom1=="!") and (chrom2!="!")'
    pairsam select 'regex_match(chrom1, "chr\d+") and regex_match(chrom2, "chr\d+")'

    '''
    select_py(
        condition, pairsam_path, output, output_rest, send_comments_to
    )
    
def select_py(
    condition, pairsam_path, output, output_rest, send_comments_to
    ):
    instream = (_fileio.auto_open(pairsam_path, mode='r') 
                if pairsam_path else sys.stdin)
    outstream = (_fileio.auto_open(output, mode='w') 
                 if output else sys.stdout)
    outstream_rest = (_fileio.auto_open(output_rest, mode='w') 
                      if output_rest else None)

    wildcard_library = {}
    def wildcard_match(x, wildcard):
        if wildcard not in wildcard_library:
            regex = fnmatch.translate(wildcard)
            reobj = re.compile(regex)
            wildcard_library[wildcard] = reobj
        return wildcard_library[wildcard].fullmatch(x)

    csv_library = {}
    def csv_match(x, csv):
        if csv not in csv_library:
            csv_library[csv] = set(csv.split(','))
        return x in csv_library[csv]

    regex_library = {}
    def regex_match(x, regex):
        if regex not in regex_library:
            reobj = re.compile(regex)
            regex_library[regex] = reobj
        return regex_library[regex].fullmatch(x)
    

    header, body_stream = _headerops.get_header(instream)
    header = _headerops.append_new_pg(header, ID=UTIL_NAME, PN=UTIL_NAME)
    outstream.writelines((l+'\n' for l in header))
    if outstream_rest:
        outstream_rest.writelines((l+'\n' for l in header))

    column_names = _headerops.extract_column_names(header)
    if len(column_names) == 0:
        column_names = _pairsam_format.COLUMNS

    condition = condition.strip()
    for i,col in enumerate(column_names):
        if col in ['pos1', 'pos2']:
            condition = condition.replace(col, 'int(COLS[{}])'.format(i))
        else:
            condition = condition.replace(col, 'COLS[{}]'.format(i))

    match_func = compile(condition, '<string>', 'eval')

    for line in body_stream:
        COLS = line.rstrip().split(_pairsam_format.PAIRSAM_SEP)
        if eval(match_func):
            outstream.write(line)
        elif outstream_rest:
            outstream_rest.write(line)

    if instream != sys.stdin:
        instream.close()

    if outstream != sys.stdout:
        outstream.close()

    if outstream_rest:
        outstream_rest.close()

if __name__ == '__main__':
    select()
