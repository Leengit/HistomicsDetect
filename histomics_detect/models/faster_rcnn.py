from histomics_detect.anchors.create import create_anchors
from histomics_detect.anchors.filter import filter_anchors
from histomics_detect.anchors.sampling import sample_anchors
from histomics_detect.boxes.transforms import parameterize
from histomics_detect.boxes.transforms import unparameterize
from histomics_detect.metrics.iou import iou
from histomics_detect.networks.fast_rcnn import fast_rcnn
from histomics_detect.networks.field_size import field_size
from histomics_detect.roialign.roialign import roialign
import tensorflow as tf


def map_outputs(output, anchors, anchor_px, field):
    """Transforms region-proposal network outputs from 3D tensors to 2D anchor arrays.
    
    The region-proposal network outputs 3D tensors containing either objectness scores 
    or parameterized regressions. Given a set of K anchor sizes, the objectness and
    regression tensors produced by the region-proposal network will have sizes 2*K 4*K
    along their third dimension respectively. This function transforms these 3D tensors
    to 2D tensors where the objectness or regressions for anchors are in rows.
        
    Parameters
    ----------
    output: tensor (float32)
        M x N x D tensor containing objectness or regression outputs from the 
        region-proposal network.
    anchors: tensor (float32)
        M*N*K x 4 tensor of anchor positions. Each row contains the x,y center 
        location of the anchor in pixel units relative in the image coordinate frame, 
        and the anchor width and height.
    anchor_px: tensor (int32)
        K-length 1-d tensor containing the anchor width hyperparameter values in pixel 
        units.
    field: float32
        Edge length of the receptive field in pixels. This defines the area of the 
        image that corresponds to 1 feature map and the anchor gridding.
        
    Returns
    -------
    mapped: tensor (float32)
        M*N*K x 2 array of objectness scores or M*N*K x 4 tensor of regressions
        where each row represents one anchor.
    """
  
    #get anchor size index for each matching anchor
    index = tf.map_fn(lambda x: tf.argmax(tf.equal(x, anchor_px),
                                          output_type=tf.int32),
                      tf.cast(anchors[:,3], tf.int32))

    #get positions of anchors in rpn output
    px = tf.cast((anchors[:,0]-field/2) / field, tf.int32)
    py = tf.cast((anchors[:,1]-field/2) / field, tf.int32)

    #add new dimension to split outputs by anchor (batch, y, x, anchor, output)
    reshaped = tf.reshape(output, tf.concat([tf.shape(output)[0:-1],
                                             [tf.size(anchor_px)],
                                             [tf.shape(output)[-1] /
                                              tf.size(anchor_px)]],
                                            axis=0))

    #gather from (batch, y, x, anchor, output) space to 2D array where each row is 
    #an anchor and columns are objectness (2-array) or regression (4-array) scores
    mapped = tf.gather_nd(reshaped,
                          tf.stack([tf.zeros(tf.shape(px), tf.int32),
                                    py, px, index],
                                   axis=1))

    return mapped


class FasterRCNN(tf.keras.Model):
    def __init__(self, rpnetwork, backbone, shape, anchor_px, lmbda, 
                 pool=2, tiles=3, **kwargs):
    
        super(FasterRCNN, self).__init__()

        #add models to self
        self.rpnetwork = rpnetwork
        self.backbone = backbone
        self.fastrcnn = fast_rcnn(backbone, tiles=tiles, pool=pool)

        #capture field, anchor sizes, loss mixing
        self.field = field_size(backbone)
        self.anchor_px = anchor_px
        self.lmbda = lmbda
        
        #capture roialign parameters
        self.pool = pool
        self.tiles = tiles

        #generate anchors for training efficiency - works for fixed-size training
        self.anchors = create_anchors(anchor_px, self.field, shape[0], shape[1])

        #define metrics
        mae_xy = tf.keras.metrics.Mean(name='mean_iou_rpn')
        mae_wh = tf.keras.metrics.Mean(name='mean_iou_align')
        auc_roc = tf.keras.metrics.AUC(curve="ROC", name='auc_roc')
        auc_pr = tf.keras.metrics.AUC(curve="PR", name='pr_roc')
        tp = tf.keras.metrics.TruePositives()
        fn = tf.keras.metrics.FalseNegatives()
        fp = tf.keras.metrics.FalsePositives()
        self.standard = [mae_xy, mae_wh, auc_roc, auc_pr, tp, fn, fp]
        
    @tf.function
    def train_step(self, data):
    
        #unpack input features, predictor and discriminator labels, and 
        #optional sample weights
        if len(data) ==3:
            rgb, boxes, sample_weight = data
        else:
            rgb, boxes = data
            sample_weight = None

        #convert boxes from RaggedTensor
        boxes = boxes.to_tensor()

        #normalize image
        norm = tf.keras.applications.resnet.preprocess_input(tf.cast(rgb, tf.float32))

        #expand dimensions
        norm = tf.expand_dims(norm, axis=0)

        #filter and sample anchors
        positive_anchors, negative_anchors = filter_anchors(boxes, self.anchors)
        positive_anchors, negative_anchors = sample_anchors(positive_anchors, negative_anchors)

        #training step
        with tf.GradientTape(persistent=True) as tape:

            #predict and capture intermediate features
            features = self.backbone(norm, training=True)
            output = self.rpnetwork(features, training=True)

            #transform outputs to 2D arrays with anchors in rows
            rpn_obj_positive = tf.nn.softmax(map_outputs(output[0], positive_anchors, 
                                                     self.anchor_px, self.field))
            rpn_obj_negative = tf.nn.softmax(map_outputs(output[0], negative_anchors, 
                                                     self.anchor_px, self.field))
            rpn_reg = map_outputs(output[1], positive_anchors, self.anchor_px,
                                  self.field)

            #generate objectness and regression labels
            rpn_obj_labels = tf.concat([tf.ones(tf.shape(rpn_obj_positive)[0], tf.uint8),
                                    tf.zeros(tf.shape(rpn_obj_negative)[0], tf.uint8)],
                                   axis=0)
            rpn_reg_label = parameterize(positive_anchors, boxes)

            #calculate objectness and regression labels
            rpn_obj_loss = self.loss[0](tf.concat([rpn_obj_positive,
                                                   rpn_obj_negative], axis=0),
                                        tf.one_hot(rpn_obj_labels, 2))
            rpn_reg_loss = self.loss[1](rpn_reg_label, rpn_reg)

            #weighted sum of objectness and regression losses
            rpn_total_loss = rpn_obj_loss / 256 + \
                rpn_reg_loss * self.lmbda / tf.cast(tf.shape(self.anchors)[0], tf.float32)
            
            #fast r-cnn regression of rpn regressions from positive anchors
            rpn_boxes = unparameterize(rpn_reg, positive_anchors)
            rpn_boxes_positive, _ = filter_anchors(boxes, rpn_boxes)
            interpolated = roialign(features, rpn_boxes_positive, self.field, 
                                    pool=self.pool, tiles=self.tiles)
            align_reg = self.fastrcnn(interpolated)
            
            #calculate fast r-cnn regression loss
            align_boxes = unparameterize(align_reg, rpn_boxes_positive)            
            align_reg_label = parameterize(rpn_boxes_positive, boxes)
            align_reg_loss = model.loss[1](align_reg_label, align_reg)

            #calculate backbone gradients and optimize
            gradients = tape.gradient(rpn_total_loss, self.backbone.trainable_weights)
            self.optimizer.apply_gradients(zip(gradients,
                                               self.backbone.trainable_weights))

            #calculate rpn gradients and optimize
            gradients = tape.gradient(rpn_total_loss, self.rpnetwork.trainable_weights)  
            self.optimizer.apply_gradients(zip(gradients,
                                               self.rpnetwork.trainable_weights))
            
            #calculate roialign gradients and optimize
            gradients = tape.gradient(align_reg_loss, self.fastrcnn.trainable_weights)  
            self.optimizer.apply_gradients(zip(gradients,
                                               self.fastrcnn.trainable_weights))
      
    
    
        #ious for rpn, roialign
        rpn_ious, _ = iou(rpn_boxes, boxes)
        rpn_ious = tf.reduce_max(rpn_ious)
        align_ious, _ = iou(align_boxes, boxes)
        align_ious = tf.reduce_max(align_ious)

        #update metrics

        self.standard[0].update_state(rpn_ious)
        self.standard[1].update_state(align_ious)
        self.standard[2].update_state(rpn_obj_labels, 
                                      tf.concat([rpn_obj_positive, rpn_obj_negative],
                                                axis=0)[:,1])
        self.standard[3].update_state(rpn_obj_labels, 
                                      tf.concat([rpn_obj_positive, rpn_obj_negative],
                                                axis=0)[:,1])
        self.standard[4].update_state(rpn_obj_labels,
                                      tf.concat([rpn_obj_positive, rpn_obj_negative],
                                                axis=0)[:,1])
        self.standard[5].update_state(rpn_obj_labels,
                                      tf.concat([rpn_obj_positive, rpn_obj_negative],
                                                axis=0)[:,1])
        self.standard[6].update_state(rpn_obj_labels,
                                      tf.concat([rpn_obj_positive, rpn_obj_negative],
                                                axis=0)[:,1])
        
        #build output dicts
        losses = {'rpn_objectness': rpn_obj_loss, 'rpn_regression': rpn_reg_loss,
                  'align_regression': align_reg_loss}
        metrics = {m.name: m.result() for m in self.standard}    

        return {**losses, **metrics}
    

class CustomCallback(tf.keras.callbacks.Callback):
    def on_epoch_end(self, epoch, logs=None):
        self.model.standard[0].reset_states()
        self.model.standard[1].reset_states()
        self.model.standard[2].reset_states()
        self.model.standard[3].reset_states()
        self.model.standard[4].reset_states()
        self.model.standard[5].reset_states()
        self.model.standard[6].reset_states()