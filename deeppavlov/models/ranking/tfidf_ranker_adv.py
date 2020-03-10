# Copyright 2017 Neural Networks and Deep Learning lab, MIPT
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from logging import getLogger
from typing import List, Any, Tuple

import numpy as np
from sklearn.preprocessing import normalize

from deeppavlov.core.common.registry import register
from deeppavlov.core.models.estimator import Component
from deeppavlov.models.vectorizers.hashing_tfidf_vectorizer import HashingTfIdfVectorizer


logger = getLogger(__name__)


@register("tfidf_ranker_adv")
class TfidfRankerAdv(Component):
    """
    Rank documents according to input strings.

    """

    def __init__(self,
                 vectorizer: HashingTfIdfVectorizer,
                 top_n=5,
                 max_n: List[int] = None,
                 active: bool = True,
                 **kwargs):

        self.max_n = max_n
        self.top_n = top_n
        self.vectorizer = vectorizer
        self.vectorizer_normalized = normalize(vectorizer.tfidf_matrix)
        self.active = active


    def __call__(self, batch_questions: List[List[str]]) -> Tuple[ List[List[str]], List[List[float]] ]:
        """
        Rank documents and return top n document titles with scores.

        """

        batch_doc_ids, batch_docs_scores = [], []

        for batch_q in batch_questions:

            # logger.debug("batch_q: " + str(batch_q))
            q_tfidfs = normalize(self.vectorizer(batch_q))

            doc_scores = []
            doc_ids = []

            max_n = list(self.max_n)
            if q_tfidfs.shape[0] < len(max_n):
                max_n[q_tfidfs.shape[0] - 1] += sum(max_n[q_tfidfs.shape[0]:])

            for j, q_tfidf in enumerate(q_tfidfs):
                top_n = max_n[j]
                # logger.debug("vector: " + str(q_tfidf))

                scores = q_tfidf * self.vectorizer_normalized
                scores = np.squeeze(
                    scores.toarray() + 0.0001)  # add a small value to eliminate zero scores

                if self.active:
                    thresh = top_n
                else:
                    thresh = len(self.vectorizer.doc_index)

                if thresh >= len(scores):
                    o = np.argpartition(-scores, len(scores) - 1)[0:thresh]
                else:
                    o = np.argpartition(-scores, thresh)[0:thresh]
                o_sort = o[np.argsort(-scores[o])]

                doc_scores.extend(scores[o_sort])
                doc_ids.extend([self.vectorizer.index2doc[i] for i in o_sort])
                # logger.debug("[docs]: " + str(doc_scores) + str(doc_ids))

            # Then we should sort doc_ids, doc_scores
            # TODO:

            batch_doc_ids.append(doc_ids)
            batch_docs_scores.append(doc_scores)

        return batch_doc_ids, batch_docs_scores
