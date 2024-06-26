use std::time::Duration;

use rust_arroyo::processing::strategies::{
    CommitRequest, ProcessingStrategy, StrategyError, SubmitError,
};
use rust_arroyo::types::Message;

pub struct Noop;

impl<T> ProcessingStrategy<T> for Noop {
    fn poll(&mut self) -> Result<Option<CommitRequest>, StrategyError> {
        Ok(None)
    }

    fn submit(&mut self, _message: Message<T>) -> Result<(), SubmitError<T>> {
        Ok(())
    }

    fn terminate(&mut self) {}

    fn join(&mut self, _timeout: Option<Duration>) -> Result<Option<CommitRequest>, StrategyError> {
        Ok(None)
    }
}
