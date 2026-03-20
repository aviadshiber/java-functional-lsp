# Functional Design Principles

Use the type system to prevent bugs at compile time rather than catching them at runtime.

## Make Illegal States Unrepresentable

```java
// BAD: Nullable fields allow illegal combinations
@Value
public class CoffeeOrder {
    DrinkType type;
    @Nullable Cream cream;
    @Nullable Whiskey whiskey;  // Irish coffee MUST have whiskey
}

// GOOD: ADT enforces invariants at compile time
public abstract class CoffeeOrder {
    private CoffeeOrder() {}
    @Value public static class Espresso extends CoffeeOrder {}
    @Value public static class Latte extends CoffeeOrder { @Nonnull Milk milk; }
    @Value public static class IrishCoffee extends CoffeeOrder {
        @Nonnull Cream cream;
        @Nonnull Whiskey whiskey;
    }
}
```

## Stop Lying to the Compiler

Make method signatures honest:
- **Null**: Declaring `String` but returning `null` → use `Option<String>`
- **Exceptions**: Claiming to return `User` but throwing → use `Either<Error, User>`

## Avoid Algebraic Blindness

Create domain-specific types when `Option.none()` has multiple meanings.

```java
// BAD: None means... never logged in? Data not loaded?
@Nonnull Option<Instant> lastLogin;

// GOOD: Domain type makes meaning explicit
public abstract class LastLogin {
    private LastLogin() {}
    @Value public static class At extends LastLogin { Instant timestamp; }
    @Value public static class Never extends LastLogin {}
}
```

## Phantom Types for Type-Safe Configurations

Use empty marker interfaces as type parameters to prevent mixing structurally identical types.

```java
@Value(staticConstructor = "of")
public static class LlmConfig<T> {
    String promptId;
    String modelName;
}

public interface Generate {}
public interface Regenerate {}

// Compiler prevents mixing:
LlmConfig<Generate> gen = LlmConfig.of("prompt1", "gpt-4");
LlmConfig<Regenerate> regen = LlmConfig.of("prompt2", "gpt-4");
// gen = regen;  // COMPILE ERROR!
```

## Error ADTs

Errors should be algebraic data types with factory methods and polymorphic behavior.

```java
public abstract class ValidationError {
    public abstract String getMessage();

    public static ValidationError notFound(Long id) {
        return new NotFoundError(id);
    }
    public static ValidationError tooShort(int length, int min) {
        return new TooShortError(length, min);
    }

    @Value public static class NotFoundError extends ValidationError {
        Long id;
        @Override public String getMessage() {
            return String.format("Not found: %d", id);
        }
    }
    @Value public static class TooShortError extends ValidationError {
        int length;
        int minLength;
        @Override public String getMessage() {
            return String.format("Too short: %d < %d", length, minLength);
        }
    }
}
```

## Stage Context Pattern

Type-safe pipeline stages with immutable context transitions.

```java
@Value public class InitialContext { String itemId; RawContent content; }
@Value public class ValidatedContext { String itemId; ValidatedContent content; }
@Value public class EnrichedContext { String itemId; ValidatedContent content; EnrichmentData enrichment; }

public Either<PipelineError, EnrichedContext> process(InitialContext initial) {
    return validateContent(initial)
        .flatMap(this::enrichWithMetadata);
}
```

## Type Parameters for Testability

Replace `void` returns with type parameters to enable pure, mockless testing.

```java
// BAD: void return requires mutable mocks
public interface Bookkeeper {
    void bookkeep(UserData original, EnrichedUserData enriched);
}

// GOOD: Type parameter enables pure functions
public interface Bookkeeper<A> {
    A bookkeep(UserData original, EnrichedUserData enriched);
}

// Test uses pure function - no mocks needed
Bookkeeper<EnrichedUserData> testBookkeeper = (orig, enriched) -> enriched;
```
