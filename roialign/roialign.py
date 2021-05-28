import tensorflow as tf
import tensorflow_addons as tfa


def _roialign_coords(box, n_points):
    """Generates arguments for bilinear interpolation of feature maps
    for a single bounding box.
    
    Used for performing roialign on regressed bounding boxes from the
    region proposal network output. This generates the coordinates
    needed to perform bilinear interpolation across the region proposal
    network feature maps at the internal grid points in the regressed
    box. Call with map_fn or vectorized_map to calculate for an array 
    containing multiple boxes.
        
    Parameters
    ----------
    box: tensor
        A 1-D tensor with 4 elements describing the x,y location of the upper 
        left corner of a regressed box and its width and height in that order.
    n_points: float
        The number of points to interpolate at across the width / height of 
        the regressed box.
        
    Returns
    -------
    sample: tensor
        The interpolation coordinate arguments in pixel units for use with 
        tfa.image.interpolate_bilinear. These are an n_points^2 x 2 array
        of x, y coordinates for interpolation that are specific to the position
        and shape of the input 'box'.
    """
    
    #generate a sequence of the indices of sample locations
    indices = tf.range(0.0, n_points)

    #calculate horizontal, vertical spacings between samples (in pixels)
    delta_width = box[2] / tf.cast(tf.size(indices), tf.float32)
    delta_height = box[3] / tf.cast(tf.size(indices), tf.float32)

    #calculate vertical and horizontal positions of sample locations
    x_offset = delta_width / 2.0 + delta_width*indices
    y_offset = delta_height / 2.0 + delta_height*indices

    #generate combinations of all pairs of x, y offsets
    x_offset = tf.stack([x_offset, tf.zeros(tf.shape(x_offset))], axis=1)
    y_offset = tf.stack([tf.zeros(tf.shape(y_offset)), y_offset], axis=1)
    offsets = tf.add(tf.expand_dims(x_offset, 0), tf.expand_dims(y_offset, 1))
    offsets = tf.reshape(offsets, [tf.shape(offsets)[0]*tf.shape(offsets)[1], 2])
    
    #add offsets to box corner x,y coordinates
    sample = tf.add(offsets, box[0:2])
    
    return sample


def roialign(features, boxes, field, pool=2, tiles=3):
    """Performs roialign on a collection of regressed bounding boxes.
    
    Given an array of N boxes, this first calculates the pixel coordinates
    where features should be interpolated within each regressed bounding box,
    then performs this interpolation and pools the results to generate a
    single feature vector for each regressed box. Each regressed box is
    subdivided into a 'tiles' x 'tiles' array, with each tile containing
    'pool' x 'pool' interpolation points. Pooling of interpolated features
    is performed at the tile level, and the resulting pooled features are
    concatenated to form a single feature vector for objectness / class
    classification.
        
    Parameters
    ----------
    features: tensor
        The three dimensional feature map tensor produced by the backbone network.
    boxes: tensor
        N x 4 tensor where each row contains the x,y location of the upper left
        corner of a regressed box and its width and height in that order.
    field: float32
        Field size of the backbone network in pixels.
    pool: int32
        pool^2 is the number of locations to interpolate features at within 
        each tile.
    tiles: int32
        tile^2 is the number of tiles that each regressed bounding box is divided
        into.
        
    Returns
    -------
    interpolated: tensor
        An N x pool * tiles x pool * tiles x features array containing the 
        interpolated features in a 2D layout grouped by bounding box. These can
        be max-pooled (pool, pool) along the spatial dimensions (dimensions 2 
        and 3) to produce an N x tile x tile x features array.
    """
    
    #generate pixel coordinates of box sampling locations
    offsets = tf.vectorized_map(lambda x: _roialign_coords(x, tiles*pool), boxes)

    #convert from pixel coordinates to feature map / receptive field coordinates
    #the top-left feature is centered at (field/2, field/2) pixels, so an offset needs
    #to be applied when comparing features which are located in the center of each
    #receptive fields with boxes that are defined by their upper left corner in the
    #image.
    offsets = (offsets - field / 2) / tf.cast(field, tf.float32)
    
    #stack offsets from all boxes into a single density * rows * cols * shape(boxes)[0] x 2 array
    #offsets = tf.concat(tf.unstack(offsets, axis=0), axis=0)
    offsets = tf.reshape(offsets, [1, -1, 2])
    
    #bilinear interpolation
    interpolated = tfa.image.interpolate_bilinear(features, offsets, indexing = 'xy')    

    #reshape so that first dimension is the box id, second/third are the spatial dimensions
    #of the subdivided box, and the last dimension is the feature
    interpolated = tf.reshape(interpolated, [tf.shape(boxes)[0],
                                             tf.cast(pool*tiles, tf.int32),
                                             tf.cast(pool*tiles, tf.int32),
                                             tf.shape(features)[-1]])

    return interpolated