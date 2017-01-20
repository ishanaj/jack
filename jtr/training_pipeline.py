# -*- coding: utf-8 -*-

import argparse
import os
import os.path as path

from time import time
import sys

import logging

import tensorflow as tf

from jtr.preprocess.batch import get_feed_dicts
from jtr.preprocess.vocab import NeuralVocab
from jtr.train import train
from jtr.util.hooks import ExamplesPerSecHook, LossHook, TensorHook, EvalHook
import jtr.nn.models as models
from jtr.load.embeddings.embeddings import load_embeddings
from jtr.pipelines import create_placeholders, pipeline

from jtr.load.read_jtr import jtr_load as _jtr_load

logger = logging.getLogger(os.path.basename(sys.argv[0]))


class Duration(object):
    def __init__(self):
        self.t0 = time()
        self.t = time()

    def __call__(self):
        logger.info('Time since last checkpoint : {0:.2g}min'.format((time()-self.t)/60.))
        self.t = time()

tf.set_random_seed(1337)
checkpoint = Duration()

"""
    Loads data, preprocesses it, and finally initializes and trains a model.

   The script does step-by-step:
      (1) Define JTR models
      (2) Parse the input arguments
      (3) Read the train, dev, and test data (with optionally loading pretrained embeddings)
      (4) Preprocesses the data (tokenize, normalize, add start and end of sentence tags) via the sisyphos.pipeline method
      (5) Create NeuralVocab
      (6) Create TensorFlow placeholders and initialize model
      (7) Batch the data via jtr.preprocess.batch.get_feed_dicts
      (8) Add hooks
      (9) Train the model
"""


def jtr_load(_path, max_count=None, **options):
    return _jtr_load(_path, max_count, **options)


def main():
    t0 = time()
    # (1) Defined JTR models
    # Please add new models to models.__models__ when they work
    reader_models = {model_name: models.get_function(model_name) for model_name in models.__models__}

    support_alts = {'none', 'single', 'multiple'}
    question_alts = answer_alts = {'single', 'multiple'}
    candidate_alts = {'open', 'per-instance', 'fixed'}

    train_default = dev_default = test_default = '../tests/test_data/sentihood/overfit.json'

    # (2) Parse the input arguments
    parser = argparse.ArgumentParser(description='Train and Evaluate a Machine Reader')

    parser.add_argument('--debug', action='store_true',
                        help="Run in debug mode, in which case the training file is also used for testing")

    parser.add_argument('--debug_examples', default=10, type=int,
                        help="If in debug mode, how many examples should be used (default 2000)")
    parser.add_argument('--train', default=train_default, type=argparse.FileType('r'), help="jtr training file")
    parser.add_argument('--dev', default=dev_default, type=argparse.FileType('r'), help="jtr dev file")
    parser.add_argument('--test', default=test_default, type=argparse.FileType('r'), help="jtr test file")
    parser.add_argument('--supports', default='single', choices=sorted(support_alts),
                        help="None, single (default) or multiple supporting statements per instance; multiple_flat reads multiple instances creates a separate instance for every support")
    parser.add_argument('--questions', default='single', choices=sorted(question_alts),
                        help="None, single (default), or multiple questions per instance")
    parser.add_argument('--candidates', default='fixed', choices=sorted(candidate_alts),
                        help="Open, per-instance, or fixed (default) candidates")
    parser.add_argument('--answers', default='single', choices=sorted(answer_alts),
                        help="Open, per-instance, or fixed (default) candidates")
    parser.add_argument('--batch_size', default=128,
        type=int, help="Batch size for training data, default 128")
    parser.add_argument('--dev_batch_size', default=128,
        type=int, help="Batch size for development data, default 128")
    parser.add_argument('--repr_dim_input', default=100, type=int,
                        help="Size of the input representation (embeddings), default 100 (embeddings cut off or extended if not matched with pretrained embeddings)")
    parser.add_argument('--repr_dim_output', default=100, type=int,
                        help="Size of the output representation, default 100")

    parser.add_argument('--pretrain', action='store_true',
                        help="Use pretrained embeddings, by default the initialisation is random")
    parser.add_argument('--train_pretrain', action='store_true',
                        help="Continue training pretrained embeddings together with model parameters")
    parser.add_argument('--normalize_pretrain', action='store_true',
                        help="Normalize pretrained embeddings, default True (randomly initialized embeddings have expected unit norm too)")

    parser.add_argument('--vocab_maxsize', default=sys.maxsize, type=int)
    parser.add_argument('--vocab_minfreq', default=2, type=int)
    parser.add_argument('--model', default='bicond_singlesupport_reader', choices=sorted(reader_models.keys()), help="Reading model to use")
    parser.add_argument('--learning_rate', default=0.001, type=float, help="Learning rate, default 0.001")
    parser.add_argument('--l2', default=0.0, type=float, help="L2 regularization weight, default 0.0")
    parser.add_argument('--clip_value', default=0.0, type=float,
                        help="Gradients clipped between [-clip_value, clip_value] (default 0.0; no clipping)")
    parser.add_argument('--drop_keep_prob', default=0.9, type=float,
                        help="Keep probability for dropout on output (set to 1.0 for no dropout)")
    parser.add_argument('--epochs', default=5, type=int, help="Number of epochs to train for, default 5")

    parser.add_argument('--tokenize', dest='tokenize', action='store_true', help="Tokenize question and support")
    parser.add_argument('--no-tokenize', dest='tokenize', action='store_false', help="Tokenize question and support")
    parser.set_defaults(tokenize=True)

    parser.add_argument('--negsamples', default=0, type=int,
                        help="Number of negative samples, default 0 (= use full candidate list)")
    parser.add_argument('--tensorboard_folder', default='./.tb/',
                        help='Folder for tensorboard logs')
    parser.add_argument('--write_metrics_to', default='',
                        help='Filename to log the metrics of the EvalHooks')
    parser.add_argument('--prune', default='False',
                        help='If the vocabulary should be pruned to the most frequent words.')

    args = parser.parse_args()

    clip_value = None
    if args.clip_value != 0.0:
        clip_value = - abs(args.clip_value), abs(args.clip_value)

    logger.info('configuration:')
    for arg in vars(args):
        logger.info('\t{} : {}'.format(str(arg), str(getattr(args, arg))))

    # (3) Read the train, dev, and test data (with optionally loading pretrained embeddings)

    embeddings = None
    if args.debug:
        train_data = jtr_load(args.train, args.debug_examples, **vars(args))

        logger.info('loaded {} samples as debug train/dev/test dataset '.format(args.debug_examples))

        dev_data = train_data
        test_data = train_data
        if args.pretrain:
            emb_file = 'glove.6B.50d.txt'
            embeddings = load_embeddings(path.join('jtr', 'data', 'GloVe', emb_file), 'glove')
            logger.info('loaded pre-trained embeddings ({})'.format(emb_file))
    else:
        train_data, dev_data, test_data = [jtr_load(name,**vars(args)) for name in [args.train, args.dev, args.test]]
        logger.info('loaded train/dev/test data')
        if args.pretrain:
            emb_file = 'GoogleNews-vectors-negative300.bin.gz'
            embeddings = load_embeddings(path.join('jtr', 'data', 'word2vec', emb_file), 'word2vec')
            logger.info('loaded pre-trained embeddings ({})'.format(emb_file))

    emb = embeddings.get if args.pretrain else None

    checkpoint()

    #  (4) Preprocesses the data (tokenize, normalize, add
    #  start and end of sentence tags) via the JTR pipeline method

    if args.vocab_minfreq != 0 and args.vocab_maxsize != 0:
        logger.info('build vocab based on train data')
        _, train_vocab, train_answer_vocab, train_candidate_vocab = pipeline(train_data, normalize=True)
        if args.prune == 'True':
            train_vocab = train_vocab.prune(args.vocab_minfreq, args.vocab_maxsize)

        logger.info('encode train data')
        train_data, _, _, _ = pipeline(train_data, train_vocab, train_answer_vocab, train_candidate_vocab, normalize=True, freeze=True)
    else:
        train_data, train_vocab, train_answer_vocab, train_candidate_vocab = pipeline(train_data, emb=emb, normalize=True, tokenization=args.tokenize, negsamples=args.negsamples)

    N_oov = train_vocab.count_oov()
    N_pre = train_vocab.count_pretrained()
    logger.info('In Training data vocabulary: {} pre-trained, {} out-of-vocab.'.format(N_pre, N_oov))

    vocab_size = len(train_vocab)
    answer_size = len(train_answer_vocab)

    # this is a bit of a hack since args are supposed to be user-defined,
    # but it's cleaner that way with passing on args to reader models
    parser.add_argument('--vocab_size', default=vocab_size, type=int)
    parser.add_argument('--answer_size', default=answer_size, type=int)
    args = parser.parse_args()

    checkpoint()
    logger.info('encode dev data')
    dev_data, _, _, _ = pipeline(dev_data, train_vocab, train_answer_vocab, train_candidate_vocab, freeze=True, tokenization=args.tokenize)
    checkpoint()
    logger.info('encode test data')
    test_data, _, _, _ = pipeline(test_data, train_vocab, train_answer_vocab, train_candidate_vocab, freeze=True, tokenization=args.tokenize)
    checkpoint()

    # (5) Create NeuralVocab

    logger.info('build NeuralVocab')
    nvocab = NeuralVocab(train_vocab, input_size=args.repr_dim_input, use_pretrained=args.pretrain,
                         train_pretrained=args.train_pretrain, unit_normalize=args.normalize_pretrain)
    checkpoint()

    # (6) Create TensorFlow placeholders and initialize model

    logger.info('create placeholders')
    placeholders = create_placeholders(train_data)
    logger.info('build model {}'.format(args.model))

    (logits, loss, predict) = reader_models[args.model](placeholders, nvocab, **vars(args))

    # (7) Batch the data via jtr.batch.get_feed_dicts

    if args.supports != "none":
        # composite buckets; first over question, then over support
        bucket_order = ('question', 'support')
        # will result in 16 composite buckets, evenly spaced over questions and supports
        bucket_structure = (4, 4)
    else:
        # question buckets
        bucket_order = ('question',)
        # 4 buckets, evenly spaced over questions
        bucket_structure = (4,)

    train_feed_dicts = \
        get_feed_dicts(train_data, placeholders, args.batch_size,
                       bucket_order=bucket_order, bucket_structure=bucket_structure)
    dev_feed_dicts = get_feed_dicts(dev_data, placeholders, args.dev_batch_size,
                                    bucket_order=bucket_order, bucket_structure=bucket_structure)

    test_feed_dicts = get_feed_dicts(test_data, placeholders, 1,
                                     bucket_order=bucket_order, bucket_structure=bucket_structure)

    optim = tf.train.AdamOptimizer(args.learning_rate)

    # little bit hacky..; for visualization of dev data during training
    dev_feed_dict = next(dev_feed_dicts.__iter__())
    sw = tf.summary.FileWriter(args.tensorboard_folder)

    answname = "targets" if "cands" in args.model else "answers"

    # (8) Add hooks

    hooks = [
        TensorHook(20, [loss, nvocab.get_embedding_matrix()],
                   feed_dicts=dev_feed_dicts, summary_writer=sw, modes=['min', 'max', 'mean_abs']),
        # report_loss
        LossHook(100, args.batch_size, summary_writer=sw),
        ExamplesPerSecHook(100, args.batch_size, summary_writer=sw),
        # evaluate on train data after each epoch
        EvalHook(train_feed_dicts, logits, predict, placeholders[answname],
                 at_every_epoch=1, metrics=['Acc', 'macroF1'],
                 print_details=False, write_metrics_to=args.write_metrics_to, info="training", summary_writer=sw),
        # evaluate on dev data after each epoch
        EvalHook(dev_feed_dicts, logits, predict, placeholders[answname],
                 at_every_epoch=1, metrics=['Acc', 'macroF1'], print_details=False,
                 write_metrics_to=args.write_metrics_to, info="development", summary_writer=sw),
        # evaluate on test data after training
        EvalHook(test_feed_dicts, logits, predict, placeholders[answname],
                 at_every_epoch=args.epochs, metrics=['Acc', 'macroP', 'macroR', 'macroF1'],
                 print_details=False, write_metrics_to=args.write_metrics_to, info="test")
    ]

    # (9) Train the model
    train(loss, optim, train_feed_dicts, max_epochs=args.epochs, l2=args.l2, clip=clip_value, hooks=hooks)
    logger.info('finished in {0:.3g}'.format((time() - t0) / 3600.))


if __name__ == "__main__":
    main()