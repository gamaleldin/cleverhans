from abc import ABCMeta
from collections import OrderedDict


class Model(object):
    """
    An abstract interface for model wrappers that exposes model symbols
    needed for making an attack. This abstraction removes the dependency on
    any specific neural network package (e.g. Keras) from the core
    code of CleverHans. It can also simplify exposing the hidden features of a
    model when a specific package does not directly expose them. For an
    example of why this is useful, see the paper "Adversarial Manipulation
    of Deep Representations" (https://arxiv.org/abs/1511.05122).
    """
    __metaclass__ = ABCMeta

    def __init__(self, model):
        """
        Init a wrapper. If `fprop_layer` is implemented, `__init__`
        should keep track of the name of the layers or `self.model` should
        provide a method for retrieving a layer.
        :param model: A function that takes a symbolic input and returns the
                      symbolic output for the model's post-softmax predictions
                      (probabilities).
        """
        self.model = model

        # The following is a cache to prevent the construction of increasingly
        # large graphs after multiple calls of the fprop methods. The cache is
        # implemented as a dictionary of the form (input, train): output_dict
        # The key is a pair of input (the symbolic representation of the input)
        # and train (a boolean indicating whether the graph is in training or
        # inference mode---which changes the behavior of layers like dropout).
        # The values output_dict are also a dictionary mapping layer names
        # to symbolic representation of the output of that layer.
        self.fprop_cache = {}

        # By default, we assume the model is being used for inference. If the
        # model is being trained, a call to set_train() should be made first.
        self.train = False

        pass

    def __call__(self, *args, **kwargs):
        """
        For compatibilty with a standard model.
        """
        return self.fprop_probs(*args, **kwargs)

    def set_train(self, train):
        """
        Define whether the model is currently being used for training or
        inference. This may change its behavior (for instance, when the model
        uses dropout layers).
        :param state: (boolean) True if the model should be in training state
                      or False for inference.
        """
        self.train = train
        state_str = 'Training' if train else 'Inference'
        print('Set the model to ' + state_str)

    def fprop_layer(self, x, layer):
        """
        Expose the hidden features of a model given a layer name.
        :param x: A symbolic representation of the network input
        :param layer: The name of the hidden layer to return features at.
        :return: A symbolic representation of the hidden features
        """
        # Return the symbolic representation for this layer.
        return self.fprop_cache[(x, self.train)][layer]

    def fprop_logits(self, x):
        """
        :param x: A symbolic representation of the network input
        :return: A symbolic representation of the output logits (i.e., the
                 values fed as inputs to the softmax layer).
        """
        raise NotImplementedError('`fprop_logits` not implemented')

    def fprop_probs(self, x):
        """
        :param x: A symbolic representation of the network input
        :return: A symbolic representation of the output probabilities (i.e.,
                the output values produced by the softmax layer).
        """
        raise NotImplementedError('`fprop_probs` not implemented')

    def get_layer_names(self):
        """
        :return: a list of names for the layers that can be exposed by this
        model abstraction.
        """
        raise NotImplementedError('`get_layer_names` not implemented')

    def fprop(self, x):
        """
        Exposes all the layers of the model returned by get_layer_names.
        :param x: A symbolic representation of the network input
        :return: A dictionary mapping layer names to the symbolic
                 representation of their output.
        """
        # In case of cache hit, return cached dictionary of output tensors.
        if (x, self.train) in self.fprop_cache.keys():
            return self.fprop_cache[(x, self.train)]

        # The implementation (missing here because this is an abstract class)
        # should populate the dictionary with all layers returned by
        # the method self.get_layer_names()
        assert all([layer in self.fprop_cache[(x, self.train)]
                    for layer in self.get_layer_names()])

        raise NotImplementedError('`fprop` not implemented')

    def get_loss(self, x, y, logits, mean=True):
        """
        Define the training loss used to train the model
        :param x: input symbol
        :param y: correct labels
        :param logits: A symbolic representation for the logits
        :param mean: boolean indicating whether should return mean of loss
                     or vector of losses for each input of the batch
        :return: return mean of loss if True, otherwise return vector with per
                 sample loss
        """
        raise NotImplementedError('`get_loss` not implemented')


class KerasModelWrapper(Model):
    """
    An implementation of `Model` that wraps a Keras model. It
    specifically exposes the hidden features of a model by creating new models.
    The symbolic graph is reused and so there is little overhead. Splitting
    in-place operations can incur an overhead.
    """

    def __init__(self, model):
        """
        Create a wrapper for a Keras model

        :param model: A Keras model
        """
        super(KerasModelWrapper, self).__init__(model)

        # Initialize attributes
        self.model = model
        # Model caching to create a new model only once for each hidden layer
        self.modelw_layer = {}
        # One model wrapper cache for `fprop`, init in the first call
        self.modelw = None

    def fprop_layer(self, x, layer):
        """
        Creates a new model with the `x` as the input and the output after the
        specified layer. Keras layers can be retrieved using their names.

        :param x: A symbolic representation of the network input
        :param layer: The name of the hidden layer
        :return: A symbolic representation of the hidden features
        """
        model = self.model

        if layer in self.modelw_layer:
            return self.modelw_layer[layer](x)

        from keras.models import Model

        # Create an extra model that exposes the hidden layer representation
        # Get input
        new_input = model.get_input_at(0)
        # Find the layer to connect
        target_feat = model.get_layer(layer).output
        # Build a new model
        new_model = Model(new_input, target_feat)
        # Cache the new model for further fprop_layer calls
        self.modelw_layer[layer] = new_model

        return new_model(x)

    def _get_softmax_name(self):
        """
        Looks for a softmax layer and if found returns the output right before
        the softmax activation.

        :return: Softmax layer name
        """
        for i, layer in enumerate(self.model.layers):
            cfg = layer.get_config()
            if 'activation' in cfg and cfg['activation'] == 'softmax':
                return layer.name

        raise Exception("No softmax layers found")

    def _get_logits_name(self):
        softmax_name = self._get_softmax_name()
        softmax_layer = self.model.get_layer(softmax_name)
        node = softmax_layer.inbound_nodes[0]
        logits_name = node.inbound_layers[0].name

        return logits_name

    def fprop_logits(self, x):
        """
        :param x: A symbolic representation of the network input.
        :return: A symbolic representation of the logits
        """
        logits_name = self._get_logits_name()

        return self.fprop_layer(x, logits_name)

    def fprop_probs(self, x):
        """
        :param x: A symbolic representation of the network input.
        :return: A symbolic representation of the probs
        """
        name = self._get_softmax_name()

        return self.fprop_layer(x, name)

    def get_layer_names(self):
        """
        :return: Names of all the layers kept by Keras
        """
        layer_names = [x.name for x in self.model.layers]
        return layer_names

    def _create_modelw(self):
        """
        Create the new model used by fprop that outputs all the hidden outputs

        :return: A new Keras model
        """
        model = self.model

        from keras.models import Model

        # Get input
        new_input = model.get_input_at(0)
        # Collect the output symbols for all the layers
        layer_names = self.get_layer_names()
        outputs = [model.get_layer(name).output for name in layer_names]
        # Build a new model
        modelw = Model(new_input, outputs)

        return modelw

    def fprop(self, x):
        """
        Creates a new model with the `x` as the input and the output after the
        specified layer. Keras layers can be retrieved using their names.

        :param x: A symbolic representation of the network input
        :return: A dictionary with keys being layer names and values being
                 symbolic representation of the output o fcorresponding layer
        """
        if self.modelw is None:
            self.modelw = self._create_modelw()
        layer_names = self.get_layer_names()
        outputs = self.modelw(x)
        out_dict = OrderedDict(zip(layer_names, outputs))
        return out_dict

    def get_loss(self, x, y, logits):
        """
        Define the TF graph for loss. Finds the logits inside the model
        and defines a cross-entropy loss on them.

        :param x: input symbol
        :param y: A symbol for correct labels
        :param logits: A symbolic representation for the logits
        :return: A TF graph for computing the loss
        """
        import tensorflow as tf
        out = tf.nn.softmax_cross_entropy_with_logits(logits=logits, labels=y)

        return out
