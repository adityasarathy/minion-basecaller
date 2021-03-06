import glob
import logging
from pprint import PrettyPrinter
import os
import shutil
import tempfile
from argparse import ArgumentParser
from configparser import ConfigParser
from dotenv import load_dotenv, find_dotenv

import h5py
import pysam
from tqdm import tqdm
from mincall import align_utils, bioinf_utils

load_dotenv(find_dotenv())

pp = PrettyPrinter()

def get_ref_starts_dict(fa_path):
    start_position = {}
    current_len = 0
    with pysam.FastxFile(fa_path) as fh:
        for entry in fh:
            start_position[entry.name] = current_len
            current_len += len(entry.sequence)
    return start_position


def reads_train_test_split(ref_root, test_size, ref_path):
    reference_len = len(bioinf_utils.read_fasta(ref_path))
    train_size = 1 - test_size

    files = glob.glob(os.path.join(ref_root, '*.ref'))
    train_path = os.path.join(ref_root, 'train.txt')
    test_path = os.path.join(ref_root, 'test.txt')

    with open(train_path, 'w') as trainf, open(test_path, 'w') as testf:
        for file_path in tqdm(files):
            basename = os.path.basename(file_path)
            name, ext = os.path.splitext(basename)

            with open(file_path, 'r') as fin:
                next(fin)  # skip header
                line = next(fin)  # line2: abs_start_pos\tstart_position\tlength
                start, rel_start, length = [int(x) for x in line.split()]
                end = start + length
                if end < reference_len * train_size:
                    # train data
                    trainf.write("%s\t%d\n" % (name, length))

                elif start > reference_len * train_size and end <= reference_len:
                    # starts after 'split' and does not overlap in case of circular aligment
                    # test data
                    testf.write("%s\t%d\n" % (name, length))
                else:
                    logging.info('Skipping ref, overlaps train and test')


def _make_rel_symlink(src, dest_dir):
    rel_src = os.path.relpath(src, dest_dir)
    dest = os.path.join(dest_dir, os.path.basename(src))
    os.symlink(rel_src, dest)


def _make_train_test_symlinks_from_list(file_list_txt, out_root, fast5_root, ref_root):
    with open(file_list_txt, 'r') as fin:
        for line in fin:
            *parts, length = line.strip().split('\t')
            name = ''.join(parts)

            fast5_path = os.path.join(fast5_root, name + '.fast5')
            ref_path = os.path.join(ref_root, name + '.ref')

            _make_rel_symlink(fast5_path, out_root)
            _make_rel_symlink(ref_path, out_root)


def _make_train_test_symlinks(train_root, test_root, fast5_root, ref_root):
    train_path = os.path.join(ref_root, 'train.txt')
    test_path = os.path.join(ref_root, 'test.txt')

    os.makedirs(train_root, exist_ok=True)
    _make_train_test_symlinks_from_list(train_path, train_root, fast5_root, ref_root)

    os.makedirs(test_root, exist_ok=True)
    _make_train_test_symlinks_from_list(test_path, test_root, fast5_root, ref_root)


def align_for_reference(fast5_in, out_dir, generate_sam_f, ref_path, batch_size):
    if os.path.isfile(fast5_in):
        file_list = [fast5_in]
    elif os.path.isdir(fast5_in):
        file_list = glob.glob(os.path.join(fast5_in, '*.fast5'))
    else:
        logging.error("Invalid fastin - expected file or dir %s, skipping!!!" % fast5_in)
        return

    n_files = len(file_list)
    os.makedirs(out_dir, exist_ok=True)
    ref_starts = get_ref_starts_dict(ref_path)

    for i in tqdm(range(0, n_files, batch_size)):
        files_in_batch = file_list[i:i + batch_size]
        _align_for_reference_batch(files_in_batch, generate_sam_f, ref_starts, out_dir)


def _align_for_reference_batch(files_in_batch, generate_sam_f, ref_starts, out_root):
    if not files_in_batch:
        return

    name_to_file = {}
    tmp_work_dir = tempfile.mkdtemp()
    tmp_sam_path = os.path.join(tmp_work_dir, 'tmp.sam')
    tmp_fastq_path = os.path.join(tmp_work_dir, 'tmp.fastq')
    with open(tmp_fastq_path, 'wb') as fq_file:
        for f in files_in_batch:
            try:
                with h5py.File(f, 'r') as h5:
                    template_key = '/Analyses/Basecall_1D_000/BaseCalled_template/Fastq'
                    if template_key not in h5:
                        logging.warning("No fastq for template found in fast5 %s", f)
                        continue
                    fastq = h5[template_key][()]
                    read_name, *_ = fastq.strip().split(b'\n')
                    read_name = read_name[1:].split(b' ')[0].decode()
                    f_name = os.path.basename(f)
                    if read_name in name_to_file:
                        logging.warning("%s duplicate name", read_name)

                    name_to_file[read_name] = f_name

                    fq_file.write(fastq)
                    fq_file.write(b'\n')

            except Exception as ex:
                logging.error('Error reading file %s', f, exc_info=True)

        generate_sam_f(tmp_fastq_path, tmp_sam_path)
        result_dict = align_utils.get_target_sequences(tmp_sam_path)

    # cleanup
    shutil.rmtree(tmp_work_dir)
    _dump_ref_files(result_dict, name_to_file, ref_starts, out_root)


def _dump_ref_files(result_dict, name_to_file, ref_starts, out_root):
    for name, (target, ref_name, start_position, length, cigar) in result_dict.items():
        basename, ext = os.path.splitext(name_to_file[name])
        ref_out_name = basename + '.ref'
        out_ref_path = os.path.join(out_root, ref_out_name)

        abs_start_pos = ref_starts[ref_name] + start_position

        with open(out_ref_path, 'w') as fout:
            fout.write('%s\t%s\n' % (name, ref_name))
            fout.write('%d\t%d\t%d\n' % (abs_start_pos, start_position, length))
            fout.write(cigar + '\n')
            fout.write(target + '\n')


def preprocess_all_ref(dataset_conf_path, only_split, align_function):
    if not os.path.isfile(dataset_conf_path):
        raise ValueError(dataset_conf_path + " doesn't exist!")
    dataset_config = ConfigParser()
    dataset_config.read(dataset_conf_path)
    batch_size = int(dataset_config['DEFAULT']['batch_size'])
    train_root = dataset_config['DEFAULT']['train_root']
    test_root = dataset_config['DEFAULT']['test_root']
    per_genome_split_root = dataset_config['DEFAULT']['per_genome_split_root']
    n_threads = int(dataset_config['DEFAULT']['n_threads'])
    for section in dataset_config.sections():
        config = dataset_config[section]

        ref_path = config['ref_path']
        fast5_root = config['fast5_root']
        ref_root = config['ref_root']
        is_circular = bool(config['circular'])
        test_size = float(config.get('test_size', -1))

        if not only_split:
            logging.info("Started %s" % section)

            def generate_sam_f(reads, sam_out):
                align_function(reads, ref_path, is_circular, sam_out, n_threads)

            logging.info("Started aligning")
            align_for_reference(fast5_root, ref_root, generate_sam_f, ref_path, batch_size)

        logging.info("Started train-test split")
        # global train-test dest for all genomes
        reads_train_test_split(ref_root, test_size, ref_path)
        _make_train_test_symlinks(train_root, test_root, fast5_root, ref_root)

        # train-test dest per genome
        logging.info("Started train-test split per genome")
        root = os.path.join(per_genome_split_root, section)
        genome_train_root = os.path.join(root, 'train')
        genome_test_root = os.path.join(root, 'test')
        _make_train_test_symlinks(genome_train_root, genome_test_root, fast5_root, ref_root)

if __name__ == '__main__':
    parser = ArgumentParser()
    parser.add_argument("dataset_conf_path", help="path to dataset conf file defining \
                                                  all references used to construct dataset", type=str)
    parser.add_argument("--split", action='store_true', help="Only split train and tests set,\
                                        ref data should be already preprocessed")
    args = parser.parse_args()

    preprocess_all_ref(args.dataset_conf_path, args.split, align_utils.align_with_graphmap)

