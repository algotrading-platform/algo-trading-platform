from abc import ABC, abstractmethod


class BaseDataProvider(ABC):

    @abstractmethod
    def fetch_data(self, symbol, interval, period):
        pass