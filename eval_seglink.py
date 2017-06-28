#encoding = utf-8

import numpy as np
import math
import tensorflow as tf # test
from tensorflow.python.ops import control_flow_ops
from tensorflow.contrib.training.python.training import evaluation
from datasets import dataset_factory
from preprocessing import ssd_vgg_preprocessing
from tf_extended import seglink, metrics as tfe_metrics, bboxes as tfe_bboxes
import util
import cv2
from nets import seglink_symbol, anchor_layer


slim = tf.contrib.slim
import config
# =========================================================================== #
# Checkpoint and running Flags
# =========================================================================== #
tf.app.flags.DEFINE_string('checkpoint_path', None, 
   'the path of checkpoint to be evaluated. If it is a directory containing many checkpoints, the lastest will be evaluated.')
tf.app.flags.DEFINE_float('gpu_memory_fraction', -1, 'the gpu memory fraction to be used. If less than 0, allow_growth = True is used.')
tf.app.flags.DEFINE_integer('batch_size', 1, 'The number of samples in each batch.')

# =========================================================================== #
# I/O and preprocessing Flags.
# =========================================================================== #
tf.app.flags.DEFINE_integer(
    'num_readers', 1,
    'The number of parallel readers that read data from the dataset.')
tf.app.flags.DEFINE_integer(
    'num_preprocessing_threads', 1,
    'The number of threads used to create the batches.')

# =========================================================================== #
# Dataset Flags.
# =========================================================================== #
tf.app.flags.DEFINE_string(
    'dataset_name', None, 'The name of the dataset to load.')
tf.app.flags.DEFINE_string(
    'dataset_split_name', 'train', 'The name of the train/test split.')
tf.app.flags.DEFINE_string(
    'dataset_dir', None, 'The directory where the dataset files are stored.')
tf.app.flags.DEFINE_string(
    'model_name', 'seglink_vgg', 'The name of the architecture to train.')
tf.app.flags.DEFINE_integer('eval_image_width', 512, 'Train image size')
tf.app.flags.DEFINE_integer('eval_image_height', 512, 'Train image size')


FLAGS = tf.app.flags.FLAGS

def config_initialization():
    # image shape and feature layers shape inference
    image_shape = (FLAGS.eval_image_height, FLAGS.eval_image_width)
    
    if not FLAGS.dataset_dir:
        raise ValueError('You must supply the dataset directory with --dataset_dir')
    tf.logging.set_verbosity(tf.logging.DEBUG)
    
    config.init_config(image_shape, batch_size = FLAGS.batch_size)

    batch_size = config.batch_size
    
    util.proc.set_proc_name('eval_' + FLAGS.model_name + '_' + FLAGS.dataset_name )
    dataset = dataset_factory.get_dataset(FLAGS.dataset_name, FLAGS.dataset_split_name, FLAGS.dataset_dir)
    config.print_config(FLAGS, dataset, print_to_file = False)
    return dataset

def create_dataset_batch_queue(dataset):
    with tf.device('/cpu:0'):
        with tf.name_scope(FLAGS.dataset_name + '_data_provider'):
            provider = slim.dataset_data_provider.DatasetDataProvider(
                dataset,
                num_readers=FLAGS.num_readers,
                common_queue_capacity=50 * config.batch_size,
                common_queue_min=30 * config.batch_size,
                shuffle=True)
            
        [image, shape, filename, glabels, gbboxes, gignored, x1, x2, x3, x4, y1, y2, y3, y4] = provider.get([
                                                         'image', 'shape', 'filename',
                                                         'object/label',
                                                         'object/bbox', 
                                                         'object/ignored',
                                                         'object/oriented_bbox/x1',
                                                         'object/oriented_bbox/x2',
                                                         'object/oriented_bbox/x3',
                                                         'object/oriented_bbox/x4',
                                                         'object/oriented_bbox/y1',
                                                         'object/oriented_bbox/y2',
                                                         'object/oriented_bbox/y3',
                                                         'object/oriented_bbox/y4'
                                                         ])
        gxs = tf.transpose(tf.stack([x1, x2, x3, x4])) #shape = (N, 4)
        gys = tf.transpose(tf.stack([y1, y2, y3, y4]))
        image = tf.identity(image, 'input_image')
        
        # Pre-processing image, labels and bboxes.
        image, glabels, gbboxes, gxs, gys = ssd_vgg_preprocessing.preprocess_image(image, glabels, gbboxes, gxs, gys, 
                                                           out_shape = config.image_shape,
                                                           data_format = config.data_format, 
                                                           is_training = False)
        image = tf.identity(image, 'processed_image')
        
        # calculate ground truth
        seg_label, seg_loc, link_gt = seglink.tf_get_all_seglink_gt(gxs, gys)
        
        # batch them
        b_image, b_seg_label, b_seg_loc, b_link_gt, b_filename, b_shape, b_ignored, b_gxs, b_gys = tf.train.batch(
            [image, seg_label, seg_loc, link_gt, filename, shape, gignored, gxs, gys],
            batch_size = config.batch_size_per_gpu,
            num_threads=FLAGS.num_preprocessing_threads,
            capacity = 50)
            
        batch_queue = slim.prefetch_queue.prefetch_queue(
            [b_image, b_seg_label, b_seg_loc, b_link_gt, b_filename, b_shape, b_ignored, b_gxs, b_gys],
            capacity = 50) 
    return batch_queue    

def eval(dataset):
    batch_queue = create_dataset_batch_queue(dataset)
    dict_metrics = {}
    
    with tf.name_scope('evaluation'):
        with tf.variable_scope(tf.get_variable_scope(), reuse = True):# the variables has been created in config.init_config
            b_image, b_seg_label, b_seg_loc, b_link_gt, b_filename, b_shape, b_gignored, b_gxs, b_gys = batch_queue.dequeue()
            net = seglink_symbol.SegLinkNet(inputs = b_image, data_format = config.data_format)
            
            # build seglink loss
            net.build_loss(seg_label = b_seg_label, seg_loc = b_seg_loc, link_label = b_link_gt,
                          seg_loc_loss_weight = 1.0, link_conf_loss_weight = 1.0, do_summary = False) # the summary will be added in the following lines
            
            # gather seglink losses
            losses = tf.get_collection(tf.GraphKeys.LOSSES)
            assert len(losses) ==  3  # 3 is the number of seglink losses: seg_cls, seg_loc, link_cls
            for loss in tf.get_collection(tf.GraphKeys.LOSSES):
                dict_metrics[loss.op.name] = slim.metrics.streaming_mean(loss)
                
            seglink_loss = tf.add_n(losses)
            dict_metrics['seglink_loss'] = slim.metrics.streaming_mean(seglink_loss)
            
            # Add metrics to summaries.
            for name, metric in dict_metrics.items():
                tf.summary.scalar(name, metric[0])
            
            # decode seglink to bbox output
            bboxes_pred = seglink.tf_seglink_to_bbox_in_batch(net.seg_scores, net.link_scores, net.seg_offsets)
            
            # calculate true positive and false positive
            num_gt_bboxes, tp_mask, fp_mask = tfe_bboxes.bboxes_matching_batch(bboxes_pred, b_gxs, b_gys, b_gignored)
            tp_fp_metric = tfe_metrics.streaming_tp_fp_arrays(num_gbboxes, tp, fp)
            dict_metrics['tp_fp'] = (tp_fp_metric[0], tp_fp_metric[1])
            
            # precision and recall
            precision, recall = tfe_metrics.precision_recall(*tp_fp_metric[0])
            fmean = tfe_metrics.fmean(precision, recall)
            tf.summary.scalar('Precision', precision)
            tf.summary.scalar('Recall', recall)
            tf.summary.scalar('fmean', fmean)
            
    names_to_values, names_to_updates = slim.metrics.aggregate_metric_map(dict_metrics)
    num_batches = int(math.ceil(dataset.num_samples / float(config.batch_size)))

    
    sess_config = tf.ConfigProto(log_device_placement = False, allow_soft_placement = True)
    if FLAGS.gpu_memory_fraction < 0:
        sess_config.gpu_options.allow_growth = True
    elif FLAGS.gpu_memory_fraction > 0:
        sess_config.gpu_options.per_process_gpu_memory_fraction = FLAGS.gpu_memory_fraction;
    
    checkpoint_dir = util.io.get_dir(FLAGS.checkpoint_path)
    logdir = util.io.join_path(FLAGS.checkpoint_path, 'eval', FLAGS.dataset_name, FLAGS.dataset_split_name)
    
    if util.io.is_dir(FLAGS.checkpoint_path):
        slim.evaluation.evaluation_loop(
            master = '',
            eval_op=list(names_to_updates.values()),
            num_evals=num_batches,
            checkpoint_path = checkpoint,
            logdir = logdir,
            session_config=sess_config)
    else:
        slim.evaluation.evaluate_once(
            master = '',
            eval_op=list(names_to_updates.values()),
            num_evals=num_batches,
            checkpoint_path = checkpoint,
            logdir = logdir,
            session_config=sess_config)

                
        
def main(_):
    eval(config_initialization())
    
    
if __name__ == '__main__':
    tf.app.run()
