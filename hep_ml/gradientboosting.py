"""
Gradient boosting is general-purpose algorithm proposed by Friedman [1].
It is one of the most efficient machine learning algorithms used for classification, regression and ranking.

The key idea of algorithm is iterative minimization of target **loss** function
by training each time one more estimator to the sequence. In this implementation decision trees are taken as such estimators.

Important difference in loss functions available in **hep_ml**.
There are for instance, loss functions to fight with correlation or loss functions for ranking.
See `hep_ml.losses` for details.

see also: XGBoost library, sklearn.ensemble.GradientBoostingClassifier

.. [1] J.H. Friedman 'Greedy function approximation: A gradient boosting machine.', 2001.
"""
from __future__ import print_function, division, absolute_import

import copy
import numpy

from sklearn.base import BaseEstimator, ClassifierMixin
from sklearn.tree.tree import DecisionTreeRegressor, DTYPE
from sklearn.utils.random import check_random_state

from .commonutils import score_to_proba, check_xyw
from .tree import SklearnClusteringTree
from .losses import AbstractLossFunction, AdaLossFunction, \
    KnnFlatnessLossFunction, BinFlatnessLossFunction, \
    SimpleKnnLossFunction, BinomialDevianceLossFunction, RankBoostLossFunction


__author__ = 'Alex Rogozhnikov'


class GradientBoostingClassifier(BaseEstimator, ClassifierMixin):
    def __init__(self, loss=None,
                 n_estimators=10,
                 learning_rate=0.1,
                 subsample=1.,
                 min_samples_split=2,
                 min_samples_leaf=1,
                 max_features=None,
                 max_leaf_nodes=None,
                 max_depth=3,
                 criterion='mse',
                 splitter='best',
                 update_tree=True,
                 train_features=None,
                 random_state=None):
        """
        This version of gradient boosting supports only two-class classification and only special losses
        derived from AbstractLossFunction.

        `max_depth`, `max_leaf_nodes`, `min_samples_leaf`, `min_samples_split`, `max_features` are parameters
        of regression tree, which is used as base estimator.

        :param loss: any descendant of AbstractLossFunction, those are very various:
            LogLossFunction, AdaLossFunction, KnnLossFunction, FlatnessLossFunction, RankBoostLossFunction.
            See hep_ml.losses for details.
        :param n_estimators: int, number of trained trees.
        :param subsample: float, fraction of data to use on each stage
        :param learning_rate: size of step.
        :param update_tree: bool, True by default. If False, 'improvement' step after fitting tree will be skipped.
        :param train_features: features used by tree.
            Note that also there may be variables used by loss function, but not used in tree.

        """
        self.loss = loss
        self.n_estimators = n_estimators
        self.learning_rate = learning_rate
        self.subsample = subsample
        self.min_samples_split = min_samples_split
        self.min_samples_leaf = min_samples_leaf
        self.max_features = max_features
        self.max_leaf_nodes = max_leaf_nodes
        self.max_depth = max_depth
        self.update_tree = update_tree
        self.train_features = train_features
        self.random_state = random_state
        self.criterion = criterion
        self.splitter = splitter
        self.classes_ = [0, 1]

    def _check_params(self):
        """Checking parameters of classifier set in __init__"""
        assert isinstance(self.loss, AbstractLossFunction), \
            'LossFunction should be derived from AbstractLossFunction'
        assert self.n_estimators > 0, 'n_estimators should be positive'
        assert 0 < self.subsample <= 1., 'subsample should be in (0, 1]'
        self.random_state = check_random_state(self.random_state)

    def _estimate_tree(self, tree, leaf_values, X):
        """taking indices of leaves and return the corresponding value for each event"""
        leaves = tree.transform(X)
        return leaf_values[leaves]

    def fit(self, X, y, sample_weight=None):
        """
        Only two-class binary classification is supported with labels 0 and 1.

        :param X: dataset of shape [n_samples, n_features]
        :param y: labels, array-like of shape [n_samples]
        :param sample_weight: array-like of shape [n_samples] or None
        :return: self
        """
        self._check_params()

        self.estimators = []
        self.scores = []

        n_samples = len(X)
        n_inbag = int(self.subsample * len(X))

        # preparing for loss function
        X, y, sample_weight = check_xyw(X, y, sample_weight=sample_weight, classification=True)
        sample_weight = sample_weight * 1. / numpy.mean(sample_weight)

        self.loss = copy.deepcopy(self.loss)
        self.loss.fit(X, y, sample_weight=sample_weight)

        # preparing for fitting in trees, setting appropriate DTYPE
        X = self._get_train_features(X)
        X = numpy.array(X, dtype=DTYPE)
        self.n_features = X.shape[1]

        y_pred = numpy.zeros(len(X), dtype=float)

        for stage in range(self.n_estimators):
            # tree creation
            tree = SklearnClusteringTree(
                criterion=self.criterion,
                splitter=self.splitter,
                max_depth=self.max_depth,
                min_samples_split=self.min_samples_split,
                min_samples_leaf=self.min_samples_leaf,
                max_features=self.max_features,
                random_state=self.random_state,
                max_leaf_nodes=self.max_leaf_nodes)

            # tree learning
            residual, weights = self.loss.prepare_tree_params(y_pred)
            train_indices = self.random_state.choice(n_samples, size=n_inbag, replace=False)

            tree.fit(X[train_indices], residual[train_indices],
                     sample_weight=weights[train_indices], check_input=False)
            # update tree leaves
            leaf_values = tree.get_leaf_values()
            if self.update_tree:
                terminal_regions = tree.transform(X)
                leaf_values = self.loss.prepare_new_leaves_values(terminal_regions, X=X, y=y, y_pred=y_pred,
                    leaf_values=leaf_values, sample_weight=sample_weight,
                    update_mask=numpy.ones(len(X), dtype=bool), residual=residual)

            y_pred += self.learning_rate * self._estimate_tree(tree, leaf_values=leaf_values, X=X)
            self.estimators.append([tree, leaf_values])
            self.scores.append(self.loss(y_pred))
        return self

    def _get_train_features(self, X):
        if self.train_features is None:
            return X
        else:
            return X.loc[:, self.train_features]

    def staged_decision_function(self, X):
        """Raw output, sum of trees' predictions after each iteration.
        :param X: data
        :return: sequence of numpy.array of shape [n_samples]
        """
        X = numpy.array(self._get_train_features(X), dtype=DTYPE)
        y_pred = numpy.zeros(len(X))
        for tree, leaf_values in self.estimators:
            y_pred += self.learning_rate * self._estimate_tree(tree, leaf_values=leaf_values, X=X)
            yield y_pred

    def decision_function(self, X):
        """Raw output, sum of trees' predictions
        :param X: data
        :return: numpy.array of shape [n_samples]
        """
        result = None
        for score in self.staged_decision_function(X):
            result = score
        return result

    def staged_predict_proba(self, X):
        """Predicted probabilities for each event
        :param X: data
        :return: sequence of numpy.array of shape [n_samples, n_classes]
        """
        for score in self.staged_decision_function(X):
            yield score_to_proba(score)

    def predict_proba(self, X):
        """Predicted probabilities for each event
        :param X: pandas.DataFrame with all train_features
        :return: numpy.array of shape [n_samples, n_classes]
        """
        return score_to_proba(self.decision_function(X))

    def predict(self, X):
        """Predicted classes for each event
        :param X: pandas.DataFrame with all train_features
        :return: numpy.array of shape [n_samples] with predicted classes.
        """
        return numpy.argmax(self.predict_proba(X), axis=1)

    @property
    def feature_importances_(self):
        """Returns feature importances for all features used in training.
        The order corresponds to the order in `self.train_features`
        :return: numpy.array of shape [n_train_features]
        """
        import warnings
        warnings.warn('feature_importances_ of gb returns importances corresponding to used columns ')
        total_sum = sum(tree.feature_importances_ for tree, values in self.estimators)
        return total_sum / len(self.estimators)