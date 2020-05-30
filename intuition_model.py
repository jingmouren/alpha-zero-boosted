from collections import defaultdict
from dataclasses import dataclass
import json
import typing
import random

import numpy
from treelite.runtime import (
    Batch as TreeliteBatch,
)

from training_samples import split_train_test
from gbdt_model import GBDTModel


class UnopinionatedValue:
    def predict(self, features):
        # :features ~ [(0, 1, ...), ...]
        return (0.0,) * len(features)


class UniformPolicy:
    def predict(self, features, allowable_actions):
        # Has to handle terminal state as well?
        if not allowable_actions:
            return {}
        uniform_probability = 1.0 / len(allowable_actions)
        return [uniform_probability] * len(allowable_actions)


@dataclass
class GBDTValue(GBDTModel):

    def extract_training_observations(self, game_samples, test_fraction):
        # :samples ~ dict(meta_info=..., features=..., labels=...)
        return split_train_test(game_samples, test_fraction)

    def train(self, samples, test_fraction=.2):
        # :samples ~ dict(meta_info=..., features=..., labels=...)
        super().train(
            objective="mean_squared_error",
            eval_metrics=["mean_squared_error", "mae"],
            samples=samples,
            test_fraction=test_fraction,
        )

    def predict(self, features) -> numpy.array:
        # :features ~ [features_1, features_2, ...]
        # :features ~ [(1, 0, ...), (0, 1, ...), ...]
        # return self.treelite_predictor.predict(batch).item(0)
        return self.treelite_predictor.predict(TreeliteBatch.from_npy2d(features)).tolist()


@dataclass
class GBDTPolicy(GBDTModel):

    def extract_policy_observations(self, features, labels):
        # features ~ [[0.0, 1.0, ...], ...]
        # labels ~ [[.01, .92, .001, ...], ...]

        # Make a training instance for every label in policy labels by
        # prepending the features for the state with the action id.
        # XXX: This will be SLOOOW. Do better. Use hstack.
        observation_features = []
        observation_labels = []
        for row_index in range(features.shape[0]):
            for i, mcts_label in enumerate(labels[row_index]):
                policy_features = numpy.concatenate(([i], features[row_index]))
                observation_features.append(policy_features)
                observation_labels.append(mcts_label)

        return (
            numpy.array(observation_features, dtype=numpy.float32),
            numpy.array(observation_labels, dtype=numpy.float32)
        )

    def extract_training_observations(self, game_samples, test_fraction):
        train_features, train_labels, test_features, test_labels = split_train_test(game_samples, test_fraction)

        # Make policy samples for each label in (features, labels) pairs
        print("\nBuilding policy training observations. Sit tight.")
        train_features, train_labels = self.extract_policy_observations(train_features, train_labels)
        test_features, test_labels = self.extract_policy_observations(test_features, test_labels)

        return (
            train_features,
            train_labels,
            test_features,
            test_labels
        )

    def train(self, samples, test_fraction=.2):
        # :samples ~ dict(meta_info=..., features=..., labels=...)
        super().train(
            objective="cross_entropy",
            eval_metrics=["cross_entropy", "mae"],
            samples=samples,
            test_fraction=test_fraction,
        )

    def predict(self, agent_features, allowable_actions):
        # :agent_features ~ array[0, 1, 0, 7, ....]
        #   - This is just ONE agent's features.  Unlike for the Value Model, every node only needs
        #     the policy of the state's *moving* agent
        # :allowable_actions ~ array[0, 1, 0, 7, ....]
        if len(allowable_actions) == 1:
            return [1.0]

        # Build ndarray with policy features
        # - tile the state features with a leading placeholder feature(s) for each action
        # - overwrite the placeholder feature(s) with action values
        # XXX: Do something besides using index as feature for model
        # XXX: Will this be slower with more allowable_actions actions than just tiling?
        num_agent_features = len(agent_features)
        to_predict = numpy.empty((len(allowable_actions), num_agent_features + 1), dtype=numpy.float32)
        for i, action in enumerate(allowable_actions):
            to_predict[i][0] = action
            to_predict[i][1:num_agent_features + 1] = agent_features[0:num_agent_features]

        # Predict move probabilities
        move_probabilities = self.treelite_predictor.predict(TreeliteBatch.from_npy2d(to_predict))

        # Normalize scores to sum to 1.0
        # - The scores returned are strong attempts at probabilities that sum up to 1.0.  In fact,
        #   they already sum up to close to 1.0 without normalization.  But because of the way the
        #   training is setup (not ovr multiclass), we need to normalize to ensure they sum to 1.0.
        move_probabilities = move_probabilities / move_probabilities.sum()
        return move_probabilities.tolist()


@dataclass
class NaiveValue:
    state_visits: typing.Any = None # features: int
    state_wins: typing.Any = None # features: int

    def save(self, output_path):
        data = {
            "state_visits": list(self.state_visits.items()),
            "state_wins": list(self.state_wins.items()),
        }

        # pathlib.Path(output_dir).mkdir(parents=True, exist_ok=True)

        with open(output_path, 'w') as f:
            f.write(json.dumps(data))

    def load(self, model_path):
        data = open(model_path, 'r').read()
        data = json.loads(data)
        self.state_visits = {tuple(key): int(value) for (key, value) in data["state_visits"]}
        self.state_wins = {tuple(key): int(value) for (key, value) in data["state_wins"]}

    def train(self, samples, test_fraction=.2):
        raise RuntimeError("Broken")
        train_set, test_set = split_train_test(samples, test_fraction, "value")

        # "Train"
        self.state_visits = defaultdict(int)
        self.state_wins = defaultdict(int)
        for features, label in train_set:
            self.state_visits[tuple(features)] += 1
            self.state_wins[tuple(features)] += label

        # Convert them to dicts to maintain consistency with load
        self.state_visits = dict(self.state_visits)
        self.state_wins = dict(self.state_wins)

        # delete any keys that are too infrequent
        to_delete = []
        for k, v in self.state_visits.items():
            if v <= 5:
                to_delete.append(k)
        for k in to_delete:
            del self.state_visits[k]
            del self.state_wins[k]

        # "Test"
        absolute_error = 0
        absolute_error_random = 0
        for features, label in test_set:
            value = self.predict(features)
            random_value = -1.0 + (2.0 * random.random())
            absolute_error += abs(label - value)
            absolute_error_random += abs(label - random_value)
        mean_absolute_error = absolute_error / len(test_set)
        mean_absolute_error_random = absolute_error_random / len(test_set)

        print("MAE:", mean_absolute_error)
        print("MAE (random):", mean_absolute_error_random)

    def predict(self, features):
        # :features ~ [(0, 1, ...), ...]
        values = []
        for board_features in features:
            try:
                values.append(self.state_wins[tuple(features)] / self.state_visits[tuple(features)])
            except (KeyError, ZeroDivisionError):
                # XXX: How is there a ZeroDivisionError but not a key error
                values.append(0)
        return tuple(values)


@dataclass
class NaivePolicy:
    state_action_mass: typing.Any = None # tuple: float
    state_action_weight: typing.Any = None # tuple: float

    def save(self, output_path):
        data = {
            "state_action_mass": list(self.state_action_mass.items()),
            "state_action_weight": list(self.state_action_weight.items()),
        }
        with open(output_path, 'w') as f:
            f.write(json.dumps(data))

    def load(self, model_path):
        data = open(model_path, 'r').read()
        data = json.loads(data)
        self.state_action_mass = {tuple(key): float(value) for (key, value) in data["state_action_mass"]}
        self.state_action_weight = {tuple(key): float(value) for (key, value) in data["state_action_weight"]}

    def train(self, samples):
        # Don't use defaultdicts so that you can distinguish the keyerror
        self.state_action_mass = {}
        self.state_action_weight = {}
        for sample_type, features, labels in samples:
            if sample_type == "value":
                continue
            # Order is determined/fixed by environment
            for i, label in enumerate(labels):
                state_action = tuple(features + [i])
                self.state_action_mass[state_action] = self.state_action_mass.get(state_action, 0.0) + label
                self.state_action_weight[state_action] = self.state_action_weight.get(state_action, 0.0) + 1.0

        # delete any keys that are too infrequent
        to_delete = []
        for k, v in self.state_action_weight.items():
            if v <= 5:
                to_delete.append(k)
        for k in to_delete:
            del self.state_action_mass[k]
            del self.state_action_weight[k]

    def predict(self, features, allowable_actions):
        try:
            move_probabilities = []
            for i, action in enumerate(allowable_actions):
                state_action = tuple(features + [i])
                move_probabilities.append(self.state_action_mass[state_action] / self.state_action_weight[state_action])
            return move_probabilities
        except KeyError:
            # Never seen this state before; therefore, use uniform policy
            # XXX: Change this to be a list like it's other predict friends.
            uniform_probability = 1.0 / len(allowable_actions)
            return [uniform_probability] * len(allowable_actions)


if __name__ == "__main__":
    pass
